'''
Train a PyTorch model
'''
import os
import shutil
import numpy as np

import torch
from torch.autograd import Variable
import torch.nn.functional as F
import sys
import model as mdls
import torchvision.utils as vutils
import utils

# Inspired by W. Kentaro (@wkentaro)
def crossentropy2d(pred, target, weight=None, ignore_index=2, size_average=True):
    '''
    Parameters
    -----------
    pred : autograd.Variable. (N, C, H, W)
        where C is number of classes.
    target : (N, H, W), where all values 0 <= target[i] <= C-1.

    Returns
    -------
    loss : Tensor.
    '''
    # pred dims (N, C, H, W)
    n, c, h, w = pred.size()
    # log_p : log_probabilities (N, C, H, W)
    log_p = F.log_softmax(pred)
    # Linearize log_p
    # log_p : (N, C, H, W) --> (N*H*W, C)
    # move dim C over twice (N, C, H, W) > (N, H, C, W) > (N, H, W, C)
    log_p = log_p.transpose(1,2).transpose(2,3).contiguous()
    # (N, H, W, C) --> (N*H*W, C)
    log_p = log_p.view(-1, c)

    # Reshape target to a (N*H*W,), where each values 0 <= i <= C-1
    target = target.view(-1)
    loss = F.nll_loss(log_p, target, weight=weight,
                        size_average=True, ignore_index=ignore_index)

    return loss

# Dice loss from Roger Trullo
# https://github.com/rogertrullo/pytorch/blob/rogertrullo-dice_loss/torch/nn/functional.py#L708
def dice_loss_integer(input_, target, ignore_label=2, C=2):
    """
    Computes a Dice loss from 2D input of class scores and a target of integer labels.

    Parameters
    ----------
    input : torch.autograd.Variable
        size B x C x H x W representing class scores.
    target : torch.autograd.Variable
        integer label representation of the ground truth, same size as the input.
    ignore_label : integer.
        Must be final label in the sequence (to do, generalize).
    C : integer.
        number of classes (including an ignored label if present!)

    Returns
    -------
    dice_total : float.
        total dice loss.
    """
    target = utils.make_one_hot(target, C=C)
    # subindex target without the ignore label
    target = target[:,:ignore_label,...]

    assert input_.size() == target.size(), "Input sizes must be equal."
    assert input_.dim() == 4, "Input must be a 4D Tensor."

    probs=F.softmax(input_)
    num=probs*target#b,c,h,w--p*g
    num=torch.sum(num,dim=3)#b,c,h
    num=torch.sum(num,dim=2)


    den1=probs*probs#--p^2
    den1=torch.sum(den1,dim=3)#b,c,h
    den1=torch.sum(den1,dim=2)


    den2=target*target#--g^2
    den2=torch.sum(den2,dim=3)#b,c,h
    den2=torch.sum(den2,dim=2)#b,c


    dice=2*(num/(den1+den2))
    dice_eso=dice[:,1:]#we ignore bg dice val, and take the fg

    dice_total=-1*torch.sum(dice_eso)/dice_eso.size(0)#divide by batch_sz

    return dice_total

def tensor_norm(T):
    return (T - T.min())/(T-T.min()).max()

class Trainer(object):
    '''
    Trains a model
    '''

    def __init__(self, model, criterion, optimizer,
                dataloaders, out_path, n_epochs=50, ignore_index=2,
                use_gpu=torch.cuda.is_available(), verbose=False, save_freq = 10,
                scheduler = None):

        '''
        Trains a PyTorch `nn.Module` object provided in `model`
        on training and testing sets provided in `dataloaders`
        using `criterion` and `optimizer`.

        Saves model weight snapshots every `save_freq` epochs and saves the
        weights with the best testing loss at the end of training.

        Parameters
        ----------
        model : torch model object, with callable `forward` method.
        criterion : callable taking inputs and targets, returning loss.
        optimizer : torch.optim optimizer.
        dataloaders : dict. train, val dataloaders keyed 'train', 'val'.
        out_path : string. output path for best model.
        n_epochs : integer. number of epochs for training.
        ignore_index : integer. class index to ignore, [0, n_class-1].
        use_gpu : boolean. use CUDA acceleration.
        verbose : boolean. write all batch losses to stdout.
        save_freq : integer. Number of epochs between model checkpoints. Default = 10.
        scheduler : learning rate scheduler.
        '''
        self.model = model
        self.optimizer = optimizer
        self.criterion = criterion
        self.n_epochs = n_epochs
        self.dataloaders = dataloaders
        self.out_path = out_path
        self.use_gpu = use_gpu
        self.ignore_index = ignore_index
        self.verbose = verbose
        self.save_freq = save_freq
        self.best_acc = 0.
        self.best_loss = 1.0e10
        self.scheduler = scheduler

        if not os.path.exists(self.out_path):
            os.mkdir(self.out_path)
        # initialize log
        self.log_path = os.path.join(self.out_path, 'log.csv')
        with open(self.log_path, 'w') as f:
            header = 'Epoch,Iter,Running_Loss,Running_Accuracy,Mode\n'
            f.write(header)

    def train_epoch(self):
        # Run a train and validation phase for each epoch
        self.model.train(True)
        i = 0
        running_loss = 0.0
        running_corrects = 0.0
        running_total = 0.0
        for data in self.dataloaders['train']:
            inputs, labels = data['image'], data['mask']
            if self.use_gpu:
                inputs = Variable(inputs.cuda())
                labels = Variable(labels.cuda())
            else:
                inputs, labels = Variable(inputs), Variable(labels)



            # zero gradients
            self.optimizer.zero_grad()

            # forward pass
            outputs = self.model(inputs)
            if self.verbose:
                self.last_output = outputs
            _, preds = torch.max(outputs.data, 1)
            loss = self.criterion(outputs, labels)
            if self.verbose:
                print('batch loss: ', loss.data[0])
            assert np.isnan(loss.data.cpu().numpy()) == False, 'NaN loss encountered in training'

            # backward pass
            loss.backward()
            self.optimizer.step()

            # statistics update
            running_loss += loss.data[0]
            corrects = (preds[labels.data != self.ignore_index].float() == labels.data[labels.data != self.ignore_index].float()).sum()
            running_corrects += corrects
            running_total += (labels.data != self.ignore_index).sum()

            if i % 100 == 0:
                print('Iter : ', i)
                print('not ignored pixels : ', running_total)
                print('running_loss : ', running_loss / (i + 1))
                print('running_acc : ', running_corrects / (running_total+1e-4)) # to avoid div by 0
                # append to log
                with open(self.log_path, 'a') as f:
                    f.write(str(self.epoch) + ',' + str(i) + ',' + str(running_loss / (i + 1)) + ',' + str(running_corrects / (running_total+1e-5)) + ',train\n')
            i += 1

        epoch_loss = running_loss / len(self.dataloaders['train'])
        epoch_acc = running_corrects / running_total
        # append to log
        with open(self.log_path, 'a') as f:
            f.write(str(self.epoch) + ',' + str(i) + ',' + str(running_loss / (i + 1)) + ',' + str(running_corrects / (running_total+1e-5)) + ',train_epoch\n')

        print('{} Loss : {:.4f} Acc: {:.4f}'.format('train', epoch_loss, epoch_acc))

    def val_epoch(self):
        self.model.train(False)
        #self.model.eval()
        i = 0
        running_loss = 0.0
        running_corrects = 0.0
        running_total = 0.0
        for data in self.dataloaders['val']:
            inputs, labels = data['image'], data['mask']
            if self.use_gpu:
                inputs = Variable(inputs.cuda(), volatile=True)
                labels = Variable(labels.cuda(), volatile=True)
            else:
                inputs, labels = Variable(inputs, volatile=True), Variable(labels, volatile=True)



            # zero gradients
            self.optimizer.zero_grad()
            # forward pass
            outputs = self.model(inputs)
            _, preds = torch.max(outputs.data, 1)
            loss = self.criterion(outputs, labels)

            # statistics update
            running_loss += loss.data[0]
            corrects = (preds[labels.data != self.ignore_index].float() == labels.data[labels.data != self.ignore_index].float()).sum()
            running_corrects += corrects
            running_total += (labels.data != self.ignore_index).sum()

            if i % 100 == 0:
                print('Iter : ', i)
                print('not ignored pixels : ', running_total)
                print('running_loss : ', running_loss / (i + 1))
                print('running_acc : ', running_corrects/(running_total + 1e-4))
                # append to log
                with open(self.log_path, 'a') as f:
                    f.write(str(self.epoch) + ',' + str(i) + ',' + str(running_loss / (i + 1)) + ',' + str(running_corrects / (running_total+1e-5)) + ',val\n')
            i += 1

        epoch_loss = running_loss / len(self.dataloaders['val'])
        epoch_acc = running_corrects / running_total
        # append to log
        with open(self.log_path, 'a') as f:
            f.write(str(self.epoch) + ',' + str(i) + ',' + str(running_loss / (i + 1)) + ',' + str(running_corrects / (running_total+1e-5)) + ',val_epoch\n')

        if epoch_loss < self.best_loss or (self.epoch%self.save_freq == 0):
                self.best_loss = epoch_loss
                self.best_model_wts = self.model.state_dict()
                torch.save(self.model.state_dict(), os.path.join(self.out_path, 'model_weights_' + str(self.epoch).zfill(3) + '.pickle'))

        print('{} Loss : {:.4f} Acc: {:.4f}'.format('val', epoch_loss, epoch_acc))


    def train(self):
        for epoch in range(self.n_epochs):
            self.epoch = epoch
            print('Epoch {}/{}'.format(epoch, self.n_epochs - 1))
            print('-' * 10)
            # run training epoch
            if self.scheduler:
                self.scheduler.step()
            self.train_epoch()
            self.val_epoch()

        print('Best val Acc: {:4f}'.format(self.best_acc))
        print('Saving best model weights...')
        torch.save(self.model.state_dict(), os.path.join(self.out_path, '00_best_model_weights.pickle'))
        print('Saved best weights.')
        self.model.load_state_dict(self.best_model_wts)
        return self.model
