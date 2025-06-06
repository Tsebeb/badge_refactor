import datetime

import numpy as np
import sys
import gc
import gzip
import pickle
#import openml
import os
import argparse
from dataset import get_dataset, get_handler
from file_output_duplicator import FileOutputDuplicator
from model import get_net
import vgg
import resnet
from sklearn.preprocessing import LabelEncoder
import torch.nn.functional as F
from torch import nn
from torchvision import transforms
import torch
import time
import pdb
from scipy.stats import zscore
import random

from query_strategies import RandomSampling, BadgeSampling, \
                                BaselineSampling, LeastConfidence, MarginSampling, \
                                EntropySampling, CoreSet, ActiveLearningByLearning, \
                                LeastConfidenceDropout, MarginSamplingDropout, EntropySamplingDropout, \
                                KMeansSampling, KCenterGreedy, BALDDropout, CoreSet, \
                                AdversarialBIM, AdversarialDeepFool, ActiveLearningByLearning, BaitSampling


parser = argparse.ArgumentParser()
parser.add_argument('--alg', help='acquisition algorithm', type=str, default='rand')
parser.add_argument('--did', help='openML dataset index, if any', type=int, default=0)
parser.add_argument('--lr', help='learning rate', type=float, default=1e-4)
parser.add_argument('--model', help='model - resnet, vgg, or mlp', type=str, default='mlp')
parser.add_argument('--path', help='data path', type=str, default='data')
parser.add_argument('--data', help='dataset (non-openML)', type=str, default='')
parser.add_argument('--nQuery', help='number of points to query in a batch', type=int, default=100)
parser.add_argument('--nStart', help='number of points to start', type=int, default=100)
parser.add_argument('--nEnd', help = 'total number of points to query', type=int, default=50000)
parser.add_argument('--nEmb', help='number of embedding dims (mlp)', type=int, default=128)
parser.add_argument('--rounds', help='number of rounds (0 does entire dataset)', type=int, default=0)
parser.add_argument('--trunc', help='dataset truncation (-1 is no truncation)', type=int, default=-1)
parser.add_argument('--aug', help='do augmentation (for cifar)', type=int, default=0)
parser.add_argument('--dummy', help='dummy input for indexing replicates', type=int, default=1)
opts = parser.parse_args()
print(opts, flush=True)


def seed_everything(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

# parameters
NUM_INIT_LB = opts.nStart
NUM_QUERY = opts.nQuery
NUM_ROUND = int((opts.nEnd - NUM_INIT_LB)/ opts.nQuery)
DATA_NAME = opts.data

# regularization settings for bait
opts.lamb = 1
if 'CIFAR' in opts.data: opts.lamb = 1e-2

date_str = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
sys.stdout = FileOutputDuplicator(sys.stdout, f'{date_str}_stdout.txt', 'w')
sys.stderr = FileOutputDuplicator(sys.stderr, f'{date_str}_stderr.txt', 'w')

# non-openml data defaults
args_pool = {'MNIST':
                {'n_epoch': 10, 'transform': transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]),
                 'loader_tr_args':{'batch_size': 64, 'num_workers': 1},
                 'loader_te_args':{'batch_size': 1000, 'num_workers': 1},
                 'optimizer_args':{'lr': 0.01, 'momentum': 0.5}},
            'FashionMNIST':
                {'n_epoch': 10, 'transform': transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))]),
                 'loader_tr_args':{'batch_size': 64, 'num_workers': 1},
                 'loader_te_args':{'batch_size': 1000, 'num_workers': 1},
                 'optimizer_args':{'lr': 0.01, 'momentum': 0.5}},
            'SVHN':
                {'n_epoch': 20, 'transform': transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.4377, 0.4438, 0.4728), (0.1980, 0.2010, 0.1970))]),
                 'loader_tr_args':{'batch_size': 64, 'num_workers': 1},
                 'loader_te_args':{'batch_size': 1000, 'num_workers': 1},
                 'optimizer_args':{'lr': 0.01, 'momentum': 0.5}},
            'CIFAR10':
                {'n_epoch': 3, 'transform': transforms.Compose([ 
                     transforms.RandomCrop(32, padding=4),
                     transforms.RandomHorizontalFlip(),
                     transforms.ToTensor(),
                     transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616))
                 ]),
                 'loader_tr_args':{'batch_size': 128, 'num_workers': 1},
                 'loader_te_args':{'batch_size': 1000, 'num_workers': 1},
                 'optimizer_args':{'lr': 0.05, 'momentum': 0.3},
                 'transformTest': transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2470, 0.2435, 0.2616))])}
                }
opts.nClasses = 10
if opts.aug == 0: 
    args_pool['CIFAR10']['transform'] =  args_pool['CIFAR10']['transformTest'] # remove data augmentation
args_pool['MNIST']['transformTest'] = args_pool['MNIST']['transform']
args_pool['FashionMNIST']['transformTest'] = args_pool['FashionMNIST']['transform']
args_pool['SVHN']['transformTest'] = args_pool['SVHN']['transform']

if opts.did == 0: args = args_pool[DATA_NAME]
if not os.path.exists(opts.path):
    os.makedirs(opts.path)


# load openml dataset if did is supplied
if opts.did > 0:
    data = pickle.load(open('oml/data_' + str(opts.did) + '.pk', 'rb'))['data']
    X = np.asarray(data[0])
    y = np.asarray(data[1])
    y = LabelEncoder().fit(y).transform(y)
    opts.nClasses = int(max(y) + 1)
    nSamps, opts.dim = np.shape(X)
    testSplit = .1
    inds = np.random.permutation(nSamps)
    X = X[inds]
    y = y[inds]


    split =int((1. - testSplit) * nSamps)
    while True:
        inds = np.random.permutation(split)
        if len(inds) > 50000: inds = inds[:50000]
        X_tr = X[:split]
        X_tr = X_tr[inds]
        X_tr = torch.Tensor(X_tr)

        y_tr = y[:split]
        y_tr = y_tr[inds]
        Y_tr = torch.Tensor(y_tr).long()

        X_te = torch.Tensor(X[split:])
        Y_te = torch.Tensor(y[split:]).long()

        if len(np.unique(Y_tr)) == opts.nClasses: break


    args = {'transform':transforms.Compose([transforms.ToTensor()]),
            'n_epoch':10,
            'loader_tr_args':{'batch_size': 128, 'num_workers': 1},
            'loader_te_args':{'batch_size': 1000, 'num_workers': 1},
            'optimizer_args':{'lr': 0.01, 'momentum': 0},
            'transformTest':transforms.Compose([transforms.ToTensor()])}
    handler = get_handler('other')

# load non-openml dataset
else:
    X_tr, Y_tr, X_te, Y_te = get_dataset(DATA_NAME, opts.path)
    opts.dim = np.shape(X_tr)[1:]
    handler = get_handler(opts.data)


if opts.trunc != -1:
   inds = np.random.permutation(len(X_tr))[:opts.trunc]
   X_tr = X_tr[inds]
   Y_tr = Y_tr[inds]
   inds = torch.where(Y_tr < 10)[0]
   X_tr = X_tr[inds]
   Y_tr = Y_tr[inds]
   opts.nClasses = int(max(Y_tr) + 1)

args['lr'] = opts.lr
args['modelType'] = opts.model
args['lamb'] = opts.lamb

# start experiment
n_pool = len(Y_tr)
n_test = len(Y_te)
print('number of labeled pool: {}'.format(NUM_INIT_LB), flush=True)
print('number of unlabeled pool: {}'.format(n_pool - NUM_INIT_LB), flush=True)
print('number of testing pool: {}'.format(n_test), flush=True)

# generate initial labeled pool
seed_everything(42)
idxs_lb = np.zeros(n_pool, dtype=bool)
indices = np.arange(n_pool)
selected_indices = np.random.choice(indices, NUM_INIT_LB, replace=False)
idxs_lb[selected_indices] = True

# linear model class
class linMod(nn.Module):
    def __init__(self, dim=28):
        super(linMod, self).__init__()
        self.dim = dim
        self.lm = nn.Linear(dim, opts.nClasses)
    def forward(self, x):
        x = x.view(-1, self.dim)
        out = self.lm(x)
        return out, x
    def get_embedding_dim(self):
        return self.dim

# mlp model class
class mlpMod(nn.Module):
    def __init__(self, dim, embSize=128, useNonLin=True):
        super(mlpMod, self).__init__()
        self.embSize = embSize
        self.dim = int(np.prod(dim))
        self.lm1 = nn.Linear(self.dim, embSize)
        self.linear = nn.Linear(embSize, opts.nClasses, bias=False)
        self.useNonLin = useNonLin
    def forward(self, x):
        x = x.view(-1, self.dim)
        if self.useNonLin: emb = F.relu(self.lm1(x))
        else: emb = self.lm1(x)
        out = self.linear(emb)
        return out, emb
    def get_embedding_dim(self):
        return self.embSize

# load specified network
seed_everything(42)
if opts.model == 'mlp':
    net = mlpMod(opts.dim, embSize=opts.nEmb)
elif opts.model == 'resnet':
    net = resnet.ResNet18()
elif opts.model == 'vgg':
    net = vgg.VGG('VGG16')
elif opts.model == 'lin':
    dim = np.prod(list(X_tr.shape[1:]))
    net = linMod(dim=dim)
else: 
    print('choose a valid model - mlp, resnet, or vgg', flush=True)
    raise ValueError
initial_state_dict = net.state_dict()


if opts.did > 0 and opts.model != 'mlp':
    print('openML datasets only work with mlp', flush=True)
    raise ValueError

if type(X_tr[0]) is not np.ndarray:
    X_tr = X_tr.numpy()

# set up the specified sampler
if opts.alg == 'rand': # random sampling
    strategy = RandomSampling(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'bait': # bait sampling
    strategy = BaitSampling(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'conf': # confidence-based sampling
    strategy = LeastConfidence(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'marg': # margin-based sampling
    strategy = MarginSampling(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'badge': # batch active learning by diverse gradient embeddings
    strategy = BadgeSampling(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'coreset': # coreset sampling
    strategy = CoreSet(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'entropy': # entropy-based sampling
    strategy = EntropySampling(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'baseline': # badge but with k-DPP sampling instead of k-means++
    strategy = BaselineSampling(X_tr, Y_tr, idxs_lb, net, handler, args)
elif opts.alg == 'albl': # active learning by learning
    albl_list = [LeastConfidence(X_tr, Y_tr, idxs_lb, net, handler, args),
        CoreSet(X_tr, Y_tr, idxs_lb, net, handler, args)]
    strategy = ActiveLearningByLearning(X_tr, Y_tr, idxs_lb, net, handler, args, strategy_list=albl_list, delta=0.1)
else: 
    print('choose a valid acquisition function', flush=True)
    raise ValueError

# print info
if opts.did > 0: DATA_NAME='OML' + str(opts.did)
print(DATA_NAME, flush=True)
print(type(strategy).__name__, flush=True)

if type(X_te) == torch.Tensor: X_te = X_te.numpy()

# round 0 accuracy
seed_everything(42)
net.load_state_dict(initial_state_dict)
strategy.train(reset=False)
P = strategy.predict(X_te, Y_te)
acc = np.zeros(NUM_ROUND+1)
acc[0] = 1.0 * (Y_te == P).sum().item() / len(Y_te)
print(str(opts.nStart) + '\ttesting accuracy {}'.format(acc[0]), flush=True)

for rd in range(1, NUM_ROUND+1):
    print("=" * 80)
    print('Round {}'.format(rd), flush=True)
    print("Number of Labelled samples: {}".format(sum(strategy.idxs_lb)), flush=True)
    print("Number of Unlabelled samples: {}".format(sum(~strategy.idxs_lb)), flush=True)
    print("=" * 80)
    torch.cuda.empty_cache()
    gc.collect()
    torch.cuda.empty_cache()
    gc.collect()

    # query
    output = strategy.query(NUM_QUERY)
    q_idxs = output
    idxs_lb[q_idxs] = True

    # update
    strategy.update(idxs_lb)
    net.load_state_dict(initial_state_dict)
    strategy.train(verbose=True, reset=False)

    # round accuracy
    P = strategy.predict(X_te, Y_te)
    acc[rd] = 1.0 * (Y_te == P).sum().item() / len(Y_te)
    print(str(sum(idxs_lb)) + '\t' + 'testing accuracy {}'.format(acc[rd]), flush=True)
    if sum(~strategy.idxs_lb) < opts.nQuery: break
    if opts.rounds > 0 and rd == (opts.rounds - 1): break

print("="*80)
print("FINAL ACCURACY PROGRESSION")
print(acc, flush=True)
print("="*80)

np.save(f"{date_str}_{opts.alg}_acc.npy", acc)
sys.exit()