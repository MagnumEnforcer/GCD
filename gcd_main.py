import argparse
from inspect import ArgSpec
import math
import numpy as np
import os
import torch

from torch.backends import cudnn
from torch import nn
from torch import optim
from torch.utils.tensorboard import SummaryWriter
from torchvision import datasets, transforms

from utils import progress_bar
from loss.spc import SupervisedContrastiveLoss
from data.cifarloader import CIFAR10Loader, CIFAR100Loader
import utils
import vision_transformer as vits
from tqdm import tqdm

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset",
        default="cifar10",
        choices=["cifar10", "cifar100"],
        help="dataset name",
    )
    parser.add_argument("--batch_size", default=128, type=int, help="On the contrastive step this will be multiplied by two.")
    parser.add_argument("--temperature", default=0.07, type=float, help="Constant for loss no thorough")
    parser.add_argument("--n_epochs", default=200, type=int)
    parser.add_argument("--lr_contrastive", default=1e-1, type=float)
    parser.add_argument("--weight_decay", type=float, default=1e-4, help="Weight decay for SGD")
    parser.add_argument("--momentum", default=0.9, type=float, help="Momentum for SGD")
    parser.add_argument("--num_workers", default=4, type=int, help="number of workers for Dataloader")
    parser.add_argument('--num_unlabeled_classes', default=5, type=int)
    parser.add_argument('--num_labeled_classes', default=5, type=int)
    parser.add_argument('--dataset_root', type=str, default='./data/datasets/CIFAR/')

    args = parser.parse_args()

    return args

def dino_vitb16_nohead(pretrained=True, **kwargs):
    """
    ViT-Base/16x16 pre-trained with DINO.
    Achieves 76.1% top-1 accuracy on ImageNet with k-NN classification.
    """
    model = vits.__dict__["vit_base"](patch_size=16, num_classes=0, **kwargs)
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url="https://dl.fbaipublicfiles.com/dino/dino_vitbase16_pretrain/dino_vitbase16_pretrain.pth",
            map_location="cpu",
        )
        model.load_state_dict(state_dict, strict=True)
    return model

def train_contrastive(model, train_loader, eval_loader, criterion, optimizer, writer, args):
    """
    :param model: torch.nn.Module Model
    :param train_loader: torch.utils.data.DataLoader
    :param criterion: torch.nn.Module Loss
    :param optimizer: torch.optim
    :param writer: torch.utils.tensorboard.SummaryWriter
    :param args: argparse.Namespace
    :return: None
    """
    model.train()
    best_loss = float("inf")
    exp_lr_scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=args.n_epochs, eta_min=0, last_epoch=-1)

    for epoch in range(args.n_epochs):
        print("Epoch [%d/%d]" % (epoch + 1, args.n_epochs))
        loss_record = utils.AverageMeter()
        train_loss = 0
        exp_lr_scheduler.step()
        for batch_idx, (x, label, idx) in enumerate(tqdm(train_loader)):
            x, label = x.to(args.device), label.to(args.device)
            output1 = model(x)
            loss= criterion(output1, label)
            loss_record.update(loss.item(), x.size(0))
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            train_loss += loss.item()
            writer.add_scalar(
                "Loss train | Supervised Contrastive",
                loss.item(),
                epoch * len(train_loader) + batch_idx,
            )
            progress_bar(
                batch_idx,
                len(train_loader),
                "Loss: %.3f " % loss_record.avg,
            )

        # Only check every 10 epochs otherwise you will always save
        if epoch % 10 == 0:
            if loss_record.avg < best_loss:
                print("Saving..")
                state = {
                    "net": model.state_dict(),
                    "avg_loss": loss_record.avg,
                    "epoch": epoch,
                }
                if not os.path.isdir("checkpoint"):
                    os.mkdir("checkpoint")
                torch.save(state, "./checkpoint/ckpt_contrastive.pth")
                best_loss = loss_record.avg

        #test(epoch, model, eval_loader, criterion, writer, args)



def test(epoch, model, test_loader, criterion, writer, args):
    """
    :param epoch: int
    :param model: torch.nn.Module, Model
    :param test_loader: torch.utils.data.DataLoader
    :param criterion: torch.nn.Module, Loss
    :param writer: torch.utils.tensorboard.SummaryWriter
    :param args: argparse.Namespace
    :return:
    """

    model.eval()
    test_loss = 0
    correct = 0
    total = 0

    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(test_loader):
            inputs, targets = inputs.to(args.device), targets.to(args.device)
            outputs = model(inputs)
            loss = criterion(outputs, targets)

            test_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()

            progress_bar(
                batch_idx,
                len(test_loader),
                "Loss: %.3f | Acc: %.3f%% (%d/%d)"
                % (
                    test_loss / (batch_idx + 1),
                    100.0 * correct / total,
                    correct,
                    total,
                ),
            )

    # Save checkpoint.
    acc = 100.0 * correct / total
    writer.add_scalar("Accuracy validation | Cross Entropy", acc, epoch)

    if acc > args.best_acc:
        print("Saving..")
        state = {
            "net": model.state_dict(),
            "acc": acc,
            "epoch": epoch,
        }
        if not os.path.isdir("checkpoint"):
            os.mkdir("checkpoint")
        torch.save(state, "./checkpoint/ckpt_cross_entropy.pth")
        args.best_acc = acc


def main():
    args = parse_args()
    device = "cuda:2" if torch.cuda.is_available() else "cpu"
    args.device = device

    if args.dataset == "cifar100":
        labeled_train_loader = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug='once', shuffle=True, target_list = range(args.num_labeled_classes))
        labeled_eval_loader = CIFAR100Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes))
        num_classes =  args.num_labeled_classes + args.num_unlabeled_classes
    else:
        labeled_train_loader = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='train', aug='once', shuffle=True, target_list = range(args.num_labeled_classes))
        labeled_eval_loader = CIFAR10Loader(root=args.dataset_root, batch_size=args.batch_size, split='test', aug=None, shuffle=False, target_list = range(args.num_labeled_classes))
        num_classes = args.num_labeled_classes + args.num_unlabeled_classes

    #load dino
    model = dino_vitb16_nohead()
    model = model.to(device)

    cudnn.benchmark = True

    if not os.path.isdir("logs"):
        os.makedirs("logs")

    writer = SummaryWriter("logs")
    
    optimizer = optim.SGD(
        model.parameters(),
        lr=args.lr_contrastive,
        momentum=args.momentum,
        weight_decay=args.weight_decay,
    )

    criterion = SupervisedContrastiveLoss(temperature=args.temperature)
    criterion.to(args.device)
    train_contrastive(model, labeled_train_loader, labeled_eval_loader, criterion, optimizer, writer, args)

    # Load checkpoint.
    print("==> Resuming from checkpoint..")
    assert os.path.isdir("checkpoint"), "Error: no checkpoint directory found!"
    checkpoint = torch.load("./checkpoint/ckpt_contrastive.pth")
    model.load_state_dict(checkpoint["net"])


if __name__ == "__main__":
    main()