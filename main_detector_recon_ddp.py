#!/usr/bin/python3
#coding=utf-8

import argparse
import os
import time
import numpy as np
from importlib import import_module
import shutil
from pathlib import Path
import sys
from tqdm import tqdm
# from tensorboardX import SummaryWriter

import torch
from torch.nn import DataParallel
from torch.nn.parallel import DistributedDataParallel
from torch.backends import cudnn
from torch.utils.data import DataLoader
from torch.utils.data.distributed import DistributedSampler

from data_detector import DataBowl3Detector, collate
# from data_detector import NoduleMalignancyDetector
# from utils import setgpu
from split_combine import SplitComb
from adable import AdaBelief

######################################################
from devkit.core.dist_utils import init_dist_slurm, init_dist, broadcast_params, average_gradients
######################################################

parser = argparse.ArgumentParser(description='PyTorch DataBowl3 Detector')
parser.add_argument('--model', '-m', metavar='MODEL', default='base',
                    help='model')
parser.add_argument('-j', '--workers', default=2, type=int, metavar='N',
                    help='number of data loading workers (default: 32)')
parser.add_argument('--epochs', default=100, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=None, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=16, type=int,
                    metavar='N', help='mini-batch size (default: 16)')
parser.add_argument('--lr', '--learning-rate', default=1e-2, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-5, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
parser.add_argument('--save-freq', default=10, type=int, metavar='S',
                    help='save frequency')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--save-dir', default=None, type=str, metavar='SAVE',
                    help='directory to save checkpoint (default: none)')
parser.add_argument('--test', default=0, type=int, metavar='TEST',
                    help='1 do test evaluation, 0 not')
parser.add_argument('--split', default=8, type=int, metavar='SPLIT',
                    help='In the test phase, split the image to 8 parts')
parser.add_argument('--gpu', default='all', type=str, metavar='N',
                    help='use gpu, "all" or "0,1,2,3" or "0,2" etc')
parser.add_argument('--n_test', default=4, type=int, metavar='N',
                    help='number of gpu for test')
parser.add_argument('--cross', default=None, type=str, metavar='N',
                    help='which data cross be used')
parser.add_argument('--cluster', action='store_true', default=False,
                    help='enables CUDA training (default: False)')

######################################################
parser.add_argument("--local_rank", type=int)
parser.add_argument('--port', default=26666, type=int, help='port of server')
parser.add_argument('--world-size', default=1, type=int)
parser.add_argument('--rank', default=0, type=int)
######################################################

args = parser.parse_args()
best_loss = 100.0

if args.cluster:
    from config_training import config_cluster as config_training
else:
    from config_training import config as config_training

use_tqdm = True

g_local_rank = -1
g_local_world_size = -1


def _log_msg(strmsg="\n"):
    global g_local_rank
    if g_local_rank == 0:
        print(strmsg)

def datestr():
    now = time.gmtime()
    return '{}{:02}{:02}_{:02}{:02}{:02}_{}'.format(now.tm_year, now.tm_mon, now.tm_mday, now.tm_hour, now.tm_min,
                                                    now.tm_sec, int(round(time.time() * 1000)))


def get_lr(epoch):
    if epoch <= 10:
        lr = 0.002
    elif epoch <= 200:
        lr = 0.001
    else:
        lr = 0.0001
    return lr

def main():
    global args, best_loss, g_local_rank, g_local_world_size

    ######################################################
    rank, world_size = init_dist_slurm(backend='nccl', port=args.port)

    g_local_rank = rank
    g_local_world_size = world_size
    print(fr"dist settings: {g_local_rank} / {g_local_world_size}")

    # torch.cuda.set_device(g_local_rank)
    # device = torch.device('cuda', g_local_rank)
    ######################################################

    datadir = config_training['preprocess_result_path']
    
    train_id = './json/' + args.cross + '/LUNA_train.json'
    val_id = './json/' + args.cross + '/LUNA_val.json'
    test_id = './json/' + args.cross + '/LUNA_test.json'

    torch.manual_seed(0)
    cudnn.benchmark = False

    # Load model
    _log_msg("=> loading model '{}'".format(args.model))
    model_root = 'net'
    model = import_module('{}.{}'.format(model_root, args.model))
    config, net, criterion, get_pbb = model.get_model(output_feature=False)

    # If possible, resume from a checkpoint
    if args.resume:
        checkpoint = torch.load(args.resume)
        net.load_state_dict(checkpoint['state_dict'])
        best_loss = checkpoint['best_loss']
        _log_msg("=> loaded checkpoint '{}' (epoch {})".format(args.resume, checkpoint['epoch']))

    # Determine the save dir
    if args.save_dir is None:
        if args.resume:
            save_dir = checkpoint['save_dir']
        else:
            exp_id = time.strftime('%Y%m%d-%H%M%S', time.localtime())
            save_dir = os.path.join('results', f'{args.model}_{exp_id}')
    else:
        exp_id = time.strftime('%Y%m%d-%H%M%S', time.localtime())
        save_dir = os.path.join('results', fr"{args.save_dir}_{exp_id}")

    # Determine the start epoch
    if args.start_epoch is None:
        if args.resume:
            start_epoch = checkpoint['epoch'] + 1
        else:
            start_epoch = 1
    else:
        start_epoch = args.start_epoch

    # If no save_dir, make a new one
    if g_local_rank == 0:
        if not os.path.isdir(save_dir):
            os.makedirs(save_dir)

    # Preserve training parameters for future analysis
    # if args.test != 1:
    #     pyfiles = list(Path('.').glob('*.py')) + list(Path('net').glob('*.py'))
    #     if not (Path(save_dir)/'net').is_dir():
    #         os.makedirs(Path(save_dir)/'net')
    #     for f in pyfiles:
    #         shutil.copy(f, Path(save_dir)/f)
            
    # Setup GPU    
    '''
    n_gpu = setgpu(args.gpu)
    args.n_gpu = n_gpu
    gpu_id = range(torch.cuda.device_count()) if args.gpu == 'all' else [int(idx.strip()) for idx in args.gpu.split(',')]        
    '''
    # net = net.to(device)
    net = net.cuda()
    # gpu_id = range(torch.cuda.device_count()) if args.gpu == 'all' else [int(idx.strip()) for idx in args.gpu.split(',')]        
    # _log_msg(fr"gpu_id {gpu_id}")
    # net = DataParallel(net, device_ids=gpu_id)

    ##############################
    broadcast_params(net)
    # net = DistributedDataParallel(net)
    ##############################
    
    # Define loss function (criterion) and optimizer
    # criterion = criterion.to(device)
    criterion = criterion.cuda()
    
    optimizer = AdaBelief(net.parameters(), lr=args.lr, weight_decay=args.weight_decay)
    pytorch_total_params = sum(p.numel() for p in net.parameters())
    _log_msg("Total number of params = {}".format(pytorch_total_params))

    #########################################################################################
    # NOT test in DDP
    # # Infer luna16's pbb/lbb, which are used in training the classifier.
    # if args.test == 1:
    #     margin = 16#16#32
    #     sidelen = 48#64#144
    #     split_comber = SplitComb(sidelen, config['max_stride'], config['stride'], margin, config['pad_value'])
    #     testset = DataBowl3Detector(datadir, test_id, config,
    #                                        phase='test', split_comber=split_comber)
    #     test_loader = DataLoader(testset, batch_size=1, shuffle=False, num_workers=0,
    #                              collate_fn=collate, pin_memory=False)
    #     test(test_loader, net, get_pbb, save_dir, config)        
    #     return
    #########################################################################################

    trainset = DataBowl3Detector(datadir, train_id, config, phase='train')
    distsampler_train = DistributedSampler(trainset)
    train_loader = DataLoader(trainset, batch_size=args.batch_size, shuffle=False, num_workers=args.workers,
                              pin_memory=True, sampler=distsampler_train)

    valset = DataBowl3Detector(datadir, val_id, config, phase='val')
    distsampler_val = DistributedSampler(valset)
    val_loader = DataLoader(valset, batch_size=1, shuffle=False, num_workers=args.workers,
                            pin_memory=True, sampler=distsampler_val)

    # run train and validate
    for epoch in range(start_epoch, args.epochs + 1):
        # Train for one epoch
        
        ##################################
        distsampler_train.set_epoch(epoch)
        ##################################
        train(train_loader, net, criterion, epoch, optimizer)

        ##################################
        distsampler_val.set_epoch(epoch)
        ##################################
        # Evaluate on validation set
        val_loss = validate(val_loader, net, criterion, epoch, save_dir)
        # Remember the best val_loss and save checkpoint
        is_best = val_loss < best_loss
        best_loss = min(val_loss, best_loss)

        if g_local_rank == 0:
            if epoch % args.save_freq == 0 or is_best:
                state_dict = net.state_dict()
                state_dict = {k:v.cpu() for k, v in state_dict.items()}
                state = {'epoch': epoch,
                        'save_dir': save_dir,
                        'state_dict': state_dict,
                        'args': args,
                        'best_loss': best_loss}
                save_checkpoint(state, is_best, os.path.join(save_dir, '{:>03d}.ckpt'.format(epoch)))

def train(data_loader, net, criterion, epoch, optimizer, lr_adjuster=None):
    start_time = time.time()

    # Switch to train mode
    net.train()
    cur_iter = int((epoch - 1) * len(data_loader)) + 1
    lr = get_lr(epoch)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr

    metrics = []
    pbar = tqdm(data_loader) if use_tqdm else data_loader
    for i, (input, target, coord) in enumerate(pbar):
        # input, target, coord = input.to(device), target.to(device), coord.to(device)
        input, target, coord = input.cuda(), target.cuda(), coord.cuda()
        # _log_msg('input.shape = ', input.shape)
        # Compute output
        output, _ = net(input, coord)
        loss = criterion(output, target, input, input, train=True)
        
        ##########################
        loss = [lloss / float(g_local_world_size) for lloss in loss]
        ##########################

        # Compute gradient and do optimizer step
        loss[0].backward()
        
        ##########################
        average_gradients(net)
        ##########################

        optimizer.step()
        optimizer.zero_grad()

        # Record the loss to metrics
        loss[0] = loss[0].item()
        metrics.append(loss)

        cur_iter += 1

    end_time = time.time()

    metrics = np.asarray(metrics, np.float32)
    eps = 1e-9
    total_postive = np.sum(metrics[:, 7])
    total_negative = np.sum(metrics[:, 9])
    total = total_postive + total_negative
    tpn = np.sum(metrics[:, 6])
    tnn = np.sum(metrics[:, 8])
    fpn = total_negative - tnn
    fnn = total_postive - tpn
    accuracy = 100.0 * (tpn + tnn) / total
    precision = 100.0 * tpn / (tpn + fpn + eps)
    recall = 100.0 * tpn / (tpn + fnn + eps)
    f1_score = 2 * precision * recall / (precision + recall + eps)
    
    _log_msg('Epoch %03d (lr %.6f)' % (epoch, lr))
    _log_msg('Train:      tpr %3.2f, tnr %3.2f, total pos %d, total neg %d, time %3.2f' % (
        100.0 * tpn / total_postive,
        100.0 * tnn / total_negative,
        total_postive,
        total_negative,
        end_time - start_time))
    _log_msg('Train:      Acc %3.2f, P %3.2f, R %3.2f, F1 %3.2f' % (
        accuracy,
        precision,
        recall,
        f1_score))
    _log_msg('loss %2.4f, classify loss %2.4f, regress loss %2.4f, %2.4f, %2.4f, %2.4f' % (
        np.mean(metrics[:, 0]),
        np.mean(metrics[:, 1]),
        np.mean(metrics[:, 2]),
        np.mean(metrics[:, 3]),
        np.mean(metrics[:, 4]),
        np.mean(metrics[:, 5]),))
    _log_msg()    

def validate(data_loader, net, criterion, epoch, save_dir):
    start_time = time.time()

    # Switch to evaluate mode
    net.eval()

    metrics = []

    pred = 0
    targ = 0
    global f1
    with torch.no_grad():
        pbar = tqdm(data_loader) if use_tqdm else data_loader
        for i, (input, target, coord) in enumerate(pbar):
            # input, target, coord = input.to(device), target.to(device), coord.to(device)
            input, target, coord = input.cuda(), target.cuda(), coord.cuda()

            # Compute output and loss
            output, _ = net(input, coord, 'val')
            loss = criterion(output, target, input, input, train=False)

            #############################
            loss = [lloss / float(g_local_world_size) for lloss in loss]
            #############################

            loss[0] = loss[0].item()
            metrics.append(loss)

    end_time = time.time()

    metrics = np.asarray(metrics, np.float32)
    eps = 1e-9
    total_postive = np.sum(metrics[:, 7])
    total_negative = np.sum(metrics[:, 9])
    total = total_postive + total_negative
    tpn = np.sum(metrics[:, 6])
    tnn = np.sum(metrics[:, 8])
    fpn = total_negative - tnn
    fnn = total_postive - tpn
    accuracy = 100.0 * (tpn + tnn) / total
    precision = 100.0 * tpn / (tpn + fpn + eps)
    recall = 100.0 * tpn / (tpn + fnn + eps)
    f1_score = 2 * precision * recall / (precision + recall + eps)

   
    _log_msg('Valid:      tpr %3.2f, tnr %3.2f, total pos %d, total neg %d, time %3.2f' % (
        100.0 * tpn / total_postive,
        100.0 * tnn / total_negative,
        total_postive,
        total_negative,
        end_time - start_time)
          )
    _log_msg('Valid:      Acc %3.2f, P %3.2f, R %3.2f, F1 %3.2f' % (
        accuracy,
        precision,
        recall,
        f1_score)
          )
    _log_msg('loss %2.4f, classify loss %2.4f, regress loss %2.4f, %2.4f, %2.4f, %2.4f' % (
        np.mean(metrics[:, 0]),
        np.mean(metrics[:, 1]),
        np.mean(metrics[:, 2]),
        np.mean(metrics[:, 3]),
        np.mean(metrics[:, 4]),
        np.mean(metrics[:, 5]),)
        )
    _log_msg()    

    val_loss = np.mean(metrics[:, 0])
    return val_loss

# def test(data_loader, net, get_pbb, save_dir, config):
    # start_time = time.time()
    # epoch = args.resume.split('/')[-1].split('.')[0]

    # bbox_dir = Path(save_dir)/'bbox'
    # bbox_dir_back = Path(save_dir)/'bbox_{}'.format(epoch)

    # if not bbox_dir.is_dir():
    #     os.makedirs(bbox_dir)

    # if not bbox_dir_back.is_dir():
    #     os.makedirs(bbox_dir_back)
    # _log_msg('Save pbb/lbb in {}'.format(bbox_dir))

    # net.eval()
    # split_comber = data_loader.dataset.split_comber

    # pbar = tqdm(data_loader) if use_tqdm else data_loader
    # for i_name, (data, target, coord, nzhw) in enumerate(pbar):
    #     target = [np.asarray(t, np.float32) for t in target]
    #     lbb = target[0]
    #     nzhw = nzhw[0]
    #     name = os.path.basename(data_loader.dataset.filenames[i_name]).split('_clean.npy')[0]        
    #     data = data[0][0]        
    #     coord = coord[0][0]        
    #     isfeat = False
    #     splitlist = list(range(0, len(data)+1, args.n_test))

    #     if splitlist[-1] != len(data):
    #         splitlist.append(len(data))

    #     outputlist = []
    #     featurelist = []

    #     with torch.no_grad():
    #         for i in range(len(splitlist)-1):
    #             input = data[splitlist[i]:splitlist[i+1]].to(device)
    #             inputcoord = coord[splitlist[i]:splitlist[i+1]].to(device)
    #             if isfeat:
    #                 feature, output, recon = net(input, inputcoord)
    #                 featurelist.append(feature.detach().cpu().numpy())
    #             else:
    #                 output, recon = net(input, inputcoord, 'val')
    #             outputlist.append(output.detach().cpu().numpy())
    #     output = np.concatenate(outputlist, axis=0)
        
    #     output = split_comber.combine(output, nzhw=nzhw)
    #     thresh = config['conf_thresh']
    #     pbb, mask = get_pbb(output, thresh, ismask=True)
    #     # Save nodule prediction
    #     np.save(os.path.join(bbox_dir, name + '_pbb.npy'), pbb)
    #     # Save nodule ground truth
    #     np.save(os.path.join(bbox_dir, name + '_lbb.npy'), lbb)
    #     np.save(os.path.join(bbox_dir_back, name + '_pbb.npy'), pbb)
    #     # Save nodule ground truth
    #     np.save(os.path.join(bbox_dir_back, name + '_lbb.npy'), lbb)

    #     if isfeat:
    #         feature = np.concatenate(featurelist,0).transpose([0,2,3,4,1])[:,:,:,:,:,np.newaxis]
    #         feature = split_comber.combine(feature, nzhw=nzhw)[...,0]
    #         feature_selected = feature[mask[0], mask[1], mask[2]]
    #         np.save(os.path.join(bbox_dir, name+'_feature.npy'), feature_selected)

    # end_time = time.time()
    # _log_msg('elapsed time is %3.2f seconds' % (end_time - start_time))
    # _log_msg()
    # _log_msg()

def save_checkpoint(state, is_best, filename):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, os.path.join(os.path.dirname(filename), 'best_loss.ckpt'))



if __name__ == '__main__':
    status = main()
    sys.exit(status)

