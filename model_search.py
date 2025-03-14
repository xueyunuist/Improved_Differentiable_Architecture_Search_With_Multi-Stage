import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from operations import *
from torch.autograd import Variable
from genotypes import PRIMITIVES
from genotypes import Genotype

class ChannelAttention(nn.Module):
  def __init__(self, in_planes, ratio=16):
    super(ChannelAttention, self).__init__()
    self.avg_pool = nn.AdaptiveAvgPool2d(1)
    self.max_pool = nn.AdaptiveMaxPool2d(1)
    # MLP
    self.fc1 = nn.Conv2d(in_planes, in_planes // 2, 1, bias=False)
    self.relu1 = nn.ReLU()
    self.fc2 = nn.Conv2d(in_planes // 2, in_planes, 1, bias=False)
    self.sigmoid = nn.Sigmoid()

  def forward(self, x):
    avg_out = self.fc2(self.relu1(self.fc1(self.avg_pool(x))))
    max_out = self.fc2(self.relu1(self.fc1(self.max_pool(x))))
    out = avg_out + max_out
    return self.sigmoid(out)

class MixedOp(nn.Module):

    def __init__(self, C, stride, switch, p, prunning_ratio):
        super(MixedOp, self).__init__()
        self.m_ops = nn.ModuleList()
        self.p = p
        self.mp = nn.MaxPool2d(2, 2)

        self.k = prunning_ratio
        self.ca = ChannelAttention(C)
        for i in range(len(switch)):
            if switch[i]:
                primitive = PRIMITIVES[i]
                op = OPS[primitive](C//self.k, stride, False)
                if 'pool' in primitive:
                    op = nn.Sequential(op, nn.BatchNorm2d(C//self.k, affine=False))
                if isinstance(op, Identity) and p > 0:
                    op = nn.Sequential(op, nn.Dropout(self.p))
                self.m_ops.append(op)
                
    def update_p(self):
        for op in self.m_ops:
            if isinstance(op, nn.Sequential):
                if isinstance(op[0], Identity):
                    op[1].p = self.p
                    
    def forward(self, x, weights):
        dim_2 = x.shape[1]
        num_list = self.ca(x)
        x = x * num_list
        num_dict = []
        slist = torch.sum(num_list, dim=0, keepdim=True)
        values, max_num_index = slist.topk(dim_2 // self.k, dim=1, largest=True, sorted=True)

        for i in range(0, len(max_num_index[0])):
            num_dict.append(max_num_index[0][i])

        xtemp = torch.index_select(x, 1, torch.tensor(num_dict).cuda())
        temp1 = sum(w * op(xtemp) for w, op in zip(weights, self.m_ops))

        if temp1.shape[2] == x.shape[2]:
            x[:, num_dict, :, :] = temp1[:, :, :, :]
        else:
            x = self.mp(x)
            x[:, num_dict, :, :] = temp1[:, :, :, :]

        return x


class Cell(nn.Module):

    def __init__(self, steps, multiplier, C_prev_prev, C_prev, C, reduction, reduction_prev, switches, p, prunning_ratio):
        super(Cell, self).__init__()
        self.reduction = reduction
        self.p = p
        self.prunning_ratio = prunning_ratio
        if reduction_prev:
            self.preprocess0 = FactorizedReduce(C_prev_prev, C, affine=False)
        else:
            self.preprocess0 = ReLUConvBN(C_prev_prev, C, 1, 1, 0, affine=False)
        self.preprocess1 = ReLUConvBN(C_prev, C, 1, 1, 0, affine=False)
        self._steps = steps
        self._multiplier = multiplier
        self.prunning_ratio = prunning_ratio
        self.cell_ops = nn.ModuleList()
        switch_count = 0
        for i in range(self._steps):
            for j in range(2+i):
                stride = 2 if reduction and j < 2 else 1
                op = MixedOp(C, stride, switch=switches[switch_count], p=self.p, prunning_ratio = self.prunning_ratio)
                self.cell_ops.append(op)
                switch_count = switch_count + 1
    
    def update_p(self):
        for op in self.cell_ops:
            op.p = self.p
            op.update_p()

    def forward(self, s0, s1, weights):
        s0 = self.preprocess0(s0)
        s1 = self.preprocess1(s1)
        states = [s0, s1]
        offset = 0
        for i in range(self._steps):
            s = sum(self.cell_ops[offset+j](h, weights[offset+j]) for j, h in enumerate(states))
            offset += len(states)
            states.append(s)

        return torch.cat(states[-self._multiplier:], dim=1)


class Network(nn.Module):

    def __init__(self, C, num_classes, layers, criterion, prunning_ratio, steps=4, multiplier=4, stem_multiplier=3, switches_normal=[], switches_reduce=[], p=0.0):
        super(Network, self).__init__()
        self._C = C
        self._num_classes = num_classes
        self._layers = layers
        self._criterion = criterion
        self._steps = steps
        self._multiplier = multiplier
        self.p = p
        self.switches_normal = switches_normal
        self.pruning_ratio = prunning_ratio
        switch_ons = []
        for i in range(len(switches_normal)):
            ons = 0
            for j in range(len(switches_normal[i])):
                if switches_normal[i][j]:
                    ons = ons + 1
            switch_ons.append(ons)
            ons = 0
        self.switch_on = switch_ons[0]

        C_curr = stem_multiplier*C
        self.stem = nn.Sequential(
            nn.Conv2d(3, C_curr, 3, padding=1, bias=False),
            nn.BatchNorm2d(C_curr)
        )
    
        C_prev_prev, C_prev, C_curr = C_curr, C_curr, C
        self.cells = nn.ModuleList()
        reduction_prev = False
        for i in range(layers):
            if i in [layers//3, 2*layers//3]:
                C_curr *= 2
                reduction = True
                cell = Cell(steps, multiplier, C_prev_prev, C_prev, C_curr, reduction, reduction_prev, switches_reduce, self.p, self.pruning_ratio)
            else:
                reduction = False
                cell = Cell(steps, multiplier, C_prev_prev, C_prev, C_curr, reduction, reduction_prev, switches_normal, self.p, self.pruning_ratio)
#            cell = Cell(steps, multiplier, C_prev_prev, C_prev, C_curr, reduction, reduction_prev, switches)
            reduction_prev = reduction
            self.cells += [cell]
            C_prev_prev, C_prev = C_prev, multiplier*C_curr

        self.global_pooling = nn.AdaptiveAvgPool2d(1)
        self.classifier = nn.Linear(C_prev, num_classes)

        self._initialize_alphas()

    def forward(self, input):
        s0 = s1 = self.stem(input)
        for i, cell in enumerate(self.cells):
            if cell.reduction:
                if self.alphas_reduce.size(1) == 1:
                    weights = F.softmax(self.alphas_reduce, dim=0)
                else:
                    weights = F.softmax(self.alphas_reduce, dim=-1)
            else:
                if self.alphas_normal.size(1) == 1:
                    weights = F.softmax(self.alphas_normal, dim=0)
                else:
                    weights = F.softmax(self.alphas_normal, dim=-1)
            s0, s1 = s1, cell(s0, s1, weights)
        out = self.global_pooling(s1)
        logits = self.classifier(out.view(out.size(0),-1))
        return logits

    def update_p(self):
        for cell in self.cells:
            cell.p = self.p
            cell.update_p()
    
    def _loss(self, input, target):
        logits = self(input)
        return self._criterion(logits, target) 

    def _initialize_alphas(self):
        k = sum(1 for i in range(self._steps) for n in range(2+i))
        num_ops = self.switch_on
        self.alphas_normal = nn.Parameter(torch.FloatTensor(1e-3*np.random.randn(k, num_ops)))
        self.alphas_reduce = nn.Parameter(torch.FloatTensor(1e-3*np.random.randn(k, num_ops)))
        self._arch_parameters = [
            self.alphas_normal,
            self.alphas_reduce,
        ]
    
    def arch_parameters(self):
        return self._arch_parameters


