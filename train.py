import argparse
import json
import os
import random

import numpy as np
import torch
import torch.nn as nn
import torch.utils.data

from lib import dataset
from lib import nets
from logger.saver import Saver
from logger import utils


def train_epoch(dataloader, model, device, optimizer, saver, epoch, accumulation_steps):
    model.train()
    sum_loss = 0
    crit_l1 = nn.L1Loss()

    for itr, (X, y, y1, y2) in enumerate(dataloader):
        saver.global_step_increment()
        X = X.to(device)
        y = y.to(device)
        y1 = y1.to(device)
        y2 = y2.to(device)
        y_pred, y1_pred, y2_pred = model.predict_fromaudio(X)
        
        X_amp = X.abs().amax(dim=(1,2)).reshape(-1,1,1) + 1e-3
        y = y / X_amp
        y1 = y1 / X_amp
        y2 = y2 / X_amp
        y_pred = y_pred / X_amp
        y1_pred = y1_pred / X_amp
        y2_pred = y2_pred / X_amp
        
        ys = model.audio2spec(y)
        ys1 = model.audio2spec(y1)
        ys2 = model.audio2spec(y2)
        ys_pred = model.audio2spec(y_pred)
        ys1_pred = model.audio2spec(y1_pred)
        ys2_pred = model.audio2spec(y2_pred)
        
        spec_loss = (crit_l1(ys, ys_pred) + crit_l1(ys1, ys1_pred) + crit_l1(ys2, ys2_pred)) / 3
        wav_loss = (crit_l1(y, y_pred) + crit_l1(y1, y1_pred) + crit_l1(y2, y2_pred)) / 3 
        loss = spec_loss + wav_loss

        current_lr =  optimizer.param_groups[0]['lr']
        saver.log_info(
                'epoch: {} | {:3d}/{:3d} | {} | batch/s: {:.2f} | lr: {:.6} | loss: {:.6f} | time: {} | step: {}'.format(
                    epoch,
                    itr,
                    len(dataloader),
                    saver.expdir,
                    1 / saver.get_interval_time(),
                    current_lr,
                    loss.item(),
                    saver.get_total_time(),
                    saver.global_step
                )
        )
        saver.log_value({
            'train/epoch': epoch, 
            'train/loss': loss.item(),
            'train/spec_loss': spec_loss.item(),  
            'train/wav_loss': wav_loss.item(),            
            'train/lr': current_lr})
        accum_loss = loss / accumulation_steps
        accum_loss.backward()

        if (itr + 1) % accumulation_steps == 0:
            optimizer.step()
            model.zero_grad()

        sum_loss += loss.item() * len(X)

    if (itr + 1) % accumulation_steps != 0:
        optimizer.step()
        model.zero_grad()

    return sum_loss / len(dataloader.dataset)


def validate_epoch(dataloader, model, device, saver):
    model.eval()

    sum_spec_loss = 0
    sum_wav_loss = 0
    sum_loss = 0
    crit_l1 = nn.L1Loss()

    with torch.no_grad():
        for X, y, y1, y2 in dataloader:
            y_pred, y1_pred, y2_pred = model.predict_fromaudio(X.to(device))
            y = y.to(device)
            y1 = y1.to(device)
            y2 = y2.to(device)
                     
            ys = model.audio2spec(y)
            ys1 = model.audio2spec(y1)
            ys2 = model.audio2spec(y2)
            ys_pred = model.audio2spec(y_pred)
            ys1_pred = model.audio2spec(y1_pred)
            ys2_pred = model.audio2spec(y2_pred)

            spec_loss = (crit_l1(ys, ys_pred) + crit_l1(ys1, ys1_pred) + crit_l1(ys2, ys2_pred)) / 3
            wav_loss = (crit_l1(y, y_pred) + crit_l1(y1, y1_pred) + crit_l1(y2, y2_pred)) / 3 
            loss = spec_loss + wav_loss

            sum_spec_loss += spec_loss.item() * len(X)
            sum_wav_loss += wav_loss.item() * len(X)
            sum_loss += loss.item() * len(X)
            
    mean_spec_loss = sum_spec_loss / len(dataloader.dataset)
    mean_wav_loss = sum_wav_loss / len(dataloader.dataset)
    mean_loss = sum_loss / len(dataloader.dataset)
    saver.log_info(' --- <validation> --- loss: {:.6f} '.format(mean_loss))
    saver.log_value({
        'validation/loss': mean_loss,
        'validation/spec_loss': mean_spec_loss,
        'validation/wav_loss': mean_wav_loss})
    return mean_loss


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--gpu', '-g', type=int, default=-1)
    p.add_argument('--seed', '-s', type=int, default=2019)
    p.add_argument('--sr', '-r', type=int, default=44100)
    p.add_argument('--hop_length', '-H', type=int, default=512)
    p.add_argument('--n_fft', '-f', type=int, default=2048)
    p.add_argument('--n_out', '-J', type=int, default=32)
    p.add_argument('--n_out_lstm', '-K', type=int, default=128)
    p.add_argument('--dataset', '-d', required=True)
    p.add_argument('--split_mode', '-S', type=str, choices=['random', 'subdirs'], default='random')
    p.add_argument('--learning_rate', '-l', type=float, default=0.0005)
    p.add_argument('--lr_min', type=float, default=0.00001)
    p.add_argument('--lr_decay_factor', type=float, default=0.9)
    p.add_argument('--lr_decay_patience', type=int, default=6)
    p.add_argument('--batchsize', '-B', type=int, default=4)
    p.add_argument('--accumulation_steps', '-A', type=int, default=1)
    p.add_argument('--cropsize', '-C', type=int, default=128)
    p.add_argument('--val_num', '-v', type=float, default=10)
    p.add_argument('--val_filelist', '-V', type=str, default=None)
    p.add_argument('--num_workers', '-w', type=int, default=4)
    p.add_argument('--epoch', '-E', type=int, default=200)
    p.add_argument('--mixup_rate', '-M', type=float, default=0.5)
    p.add_argument('--mixup_alpha', '-a', type=float, default=1.0)
    p.add_argument('--pretrained_model', '-P', type=str, default=None)
    p.add_argument('--exp_name', '-N', type=str, default="model_test")
    p.add_argument('--mono', action='store_true')
    p.add_argument('--debug', action='store_true')
    args = p.parse_args()

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    val_filelist = []
    if args.val_filelist is not None:
        with open(args.val_filelist, 'r', encoding='utf8') as f:
            val_filelist = json.load(f)

    train_filelist, val_filelist = dataset.train_val_split(
        dataset_dir=args.dataset,
        split_mode=args.split_mode,
        val_num=args.val_num,
        val_filelist=val_filelist
    )

    if args.debug:
        train_filelist = train_filelist[:1]
        val_filelist = val_filelist[:1]

    device = torch.device('cpu')
    model = nets.CascadedNet(args.n_fft, args.hop_length, args.n_out, args.n_out_lstm, True, is_mono=args.mono)
    if args.pretrained_model is not None:
        print("loading pretrained model: "+ args.pretrained_model)
        model.load_state_dict(torch.load(args.pretrained_model, map_location=device))
    if torch.cuda.is_available() and args.gpu >= 0:
        device = torch.device('cuda:{}'.format(args.gpu))
        model.to(device)

    optimizer = torch.optim.Adam(
        filter(lambda p: p.requires_grad, model.parameters()),
        lr=args.learning_rate
    )

    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer,
        factor=args.lr_decay_factor,
        patience=args.lr_decay_patience,
        threshold=1e-6,
        min_lr=args.lr_min,
        verbose=True
    )

    train_dataset = dataset.VocalRemoverTrainingSet(
        train_filelist,
        sr=args.sr,
        hop_length=args.hop_length,
        cropsize=args.cropsize,
        mixup_rate=args.mixup_rate,
        mixup_alpha=args.mixup_alpha
    )

    train_dataloader = torch.utils.data.DataLoader(
        dataset=train_dataset,
        batch_size=args.batchsize,
        shuffle=True,
        num_workers=args.num_workers,
        persistent_workers=(args.num_workers > 0),
        pin_memory=True
    )
        
    val_dataset = dataset.VocalRemoverValidationSet(
        val_filelist,
        sr=args.sr
    )

    val_dataloader = torch.utils.data.DataLoader(
        dataset=val_dataset,
        batch_size=1,
        shuffle=False,
        num_workers=0,
        pin_memory=True
    )
    
    saver = Saver(args)
    
    params_count = utils.get_network_paras_amount({'model': model})
    saver.log_info('--- model size ---')
    saver.log_info(params_count)
    
    for epoch in range(args.epoch):
        train_loss = train_epoch(train_dataloader, model, device, optimizer, saver, epoch, args.accumulation_steps)
        val_loss = validate_epoch(val_dataloader, model, device, saver)

        scheduler.step(val_loss)

        saver.save_model(model, postfix=str(epoch))


if __name__ == '__main__':
    main()
