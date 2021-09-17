import os
import sys
import random
import argparse
import multiprocessing
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.optim.lr_scheduler import ExponentialLR
from torch.utils.data import Dataset, DataLoader , Subset, random_split

from dataset import *
from model import *
from preprocessor import *

def progressLearning(value, endvalue, loss, acc, bar_length=50):
    percent = float(value + 1) / endvalue
    arrow = '-' * int(round(percent * bar_length)-1) + '>'
    spaces = ' ' * (bar_length - len(arrow))
    sys.stdout.write("\rPercent: [{0}] {1}/{2} \t Loss : {3:.3f}, Acc : {4:.3f}".format(arrow + spaces, 
        value+1, 
        endvalue, 
        loss, 
        acc)
    )
    sys.stdout.flush()

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)

def train(args) :
    # -- Seed
    seed_everything(args.seed)

    # -- Device
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # -- Text Data
    text_data = pd.read_csv(args.data_dir)
    text_list = list(text_data['번역문'])

    # -- Tokenize & Encoder
    en_text_path = os.path.join(args.token_dir, 'english.txt')
    if os.path.exists(en_text_path) == False :
        write_data(text_list, en_text_path, preprocess_en)
    en_spm = get_spm(args.token_dir, 'english.txt', 'en_spm', args.token_size)

    idx_data = []
    for sen in text_list :
        sen = preprocess_en(sen)
        idx_list = en_spm.encode_as_ids(sen)
        idx_data.append(idx_list)

    # -- Dataset
    ngram_dset = NgramDataset(args.token_size, args.window_size)
    cen_data, con_data = ngram_dset.get_data(idx_data)
    skipgram_dset = SkipGramDataset(cen_data, con_data, args.val_ratio)
    train_dset, val_dset = skipgram_dset.split()

    # -- DataLoader
    train_loader = DataLoader(train_dset,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=True,
        batch_size=args.batch_size
    )
    val_loader = DataLoader(val_dset,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=False,
        batch_size=args.val_batch_size
    )
    
    # -- Model
    model = SkipGram(args.embedding_size, args.token_size, args.window_size).to(device)

    # -- Optimizer
    optimizer = optim.Adam(model.parameters(), lr=args.lr)

    # -- Scheduler
    scheduler = optim.lr_scheduler.ExponentialLR(optimizer, gamma=0.7)
    
    # -- Loss
    criterion = nn.CrossEntropyLoss().to(device)

    # -- Training
    min_loss = np.inf
    stop_count = 0
    for epoch in range(args.epochs) :
        idx = 0
        model.train()
        print('Epoch : %d/%d \t Learning Rate : %e' %(epoch, args.epochs, optimizer.param_groups[0]["lr"]))
        for cen_in, con_label in train_loader :
            cen_in = cen_in.long().to(device)
            con_label = con_label.long().to(device)
            con_label = con_label.view([-1,])

            con_output = model(cen_in)
            con_output = con_output.view([-1, args.token_size])

            loss = criterion(con_output, con_label)
            acc = (torch.argmax(con_output,-1) == con_label).float().mean()
            loss.backward()
            optimizer.step()
        
            progressLearning(idx, len(train_loader), loss.item(), acc.item())
            idx += 1

        with torch.no_grad() :
            model.eval()
            val_loss = 0.0
            val_acc = 0.0
            for cen_in, con_label in val_loader :
                cen_in = cen_in.long().to(device)
                con_label = con_label.long().to(device)
                con_label = con_label.view([-1,])

                con_output = model(cen_in)
                con_output = con_output.view([-1, args.token_size])

                loss = criterion(con_output, con_label)
                acc = (torch.argmax(con_output,-1) == con_label).float().mean()
                val_loss += loss
                val_acc += acc

            val_loss /= len(val_loader)
            val_acc /= len(val_loader)

        if val_loss < min_loss :
            min_loss = val_loss
            torch.save({'epoch' : (epoch) ,  
                'model_state_dict' : model.state_dict() , 
                'loss' : val_loss.item()}, 
            os.path.join(args.model_dir,'en_skipgram.pt'))
            stop_count = 0
        else :
            stop_count += 1
            if stop_count >= 5 :
                print('\nTraining Early Stopped') 
                break

        scheduler.step()
        print('\nVal Loss : %.3f , Val Acc : %.3f\n' %(val_loss, val_acc))


    en_weight = model.get_weight()
    en_weight = en_weight.detach().cpu().numpy()

    en_bias = model.get_bias()
    en_bias = en_bias.detach().cpu().numpy()

    np.save(os.path.join(args.embedding_dir, 'en_weight.npy'), en_weight)
    np.save(os.path.join(args.embedding_dir, 'en_bias.npy'), en_bias)

if __name__ == '__main__' :
    parser = argparse.ArgumentParser()

    parser.add_argument('--seed', type=int, default=777, help='random seed (default: 777)')
    parser.add_argument('--epochs', type=int, default=20, help='number of epochs to train (default: 30)')
    parser.add_argument('--token_size', type=int, default=7000, help='number of bpe merge (default: 7000)')
    parser.add_argument('--embedding_size', type=int, default=512, help='embedding size of token (default: 512)')
    parser.add_argument('--window_size', type=int, default=11, help='window size (default: 11)')
    parser.add_argument('--batch_size', type=int, default=1024, help='input batch size for training (default: 1024)')
    parser.add_argument('--val_batch_size', type=int, default=1024, help='input batch size for validing (default: 1024)')
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate (default: 1e-4)')    
    parser.add_argument('--val_ratio', type=float, default=0.1, help='ratio for validaton (default: 0.1)')

    parser.add_argument('--data_dir', type=str, default='../Data/korean_dialogue_translation.csv', help = 'text data')
    parser.add_argument('--token_dir', type=str, default='./Token' , help='token data dir path')
    parser.add_argument('--embedding_dir', type=str, default='./Embedding' , help='embedding dir path')
    parser.add_argument('--model_dir', type=str, default='./Model' , help='best model dir path')

    args = parser.parse_args()

    train(args)
