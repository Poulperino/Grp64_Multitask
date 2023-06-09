"""Train a neural net."""

import argparse
import copy
import hashlib
import logging
import shutil
import time

############################################################################################################
import math
############################################################################################################

import torch

from ..profiler import TorchProfiler

LOG = logging.getLogger(__name__)


class Trainer():
    epochs = None
    n_train_batches = None
    n_val_batches = None

    clip_grad_norm = 0.0
    clip_grad_value = 0.0
    log_interval = 11
    val_interval = 1

    fix_batch_norm = False
    stride_apply = 1
    ema_decay = 0.01
    train_profile = None
    distributed_reduce_loss = True

    def __init__(self, model, loss, optimizer, out, *,
                 checkpoint_shell=None,
                 lr_scheduler=None,
                 device=None,
                 model_meta_data=None):
        self.model = model
        self.loss = loss
        self.optimizer = optimizer
        self.out = out
        self.checkpoint_shell = checkpoint_shell
        self.lr_scheduler = lr_scheduler
        self.device = device
        self.model_meta_data = model_meta_data

        ################################################################################################################################################
        #Adding a list to save previous head losses
        self.head_loss_array = []
        self.K = 1 #arbitrary
        self.T = 2 #arbitrary
        self.start_epoch = -1
        self.avg_loss = []
        ################################################################################################################################################

        self.ema = None
        self.ema_restore_params = None

        self.n_clipped_grad = 0
        self.max_norm = 0.0

        if self.train_profile and (not torch.distributed.is_initialized()
                                   or torch.distributed.get_rank() == 0):
            # monkey patch to profile self.train_batch()
            self.train_batch = TorchProfiler(self.train_batch, out_name=self.train_profile)

        LOG.info({
            'type': 'config',
            'field_names': self.loss.field_names,
        })

    @classmethod
    def cli(cls, parser: argparse.ArgumentParser):
        group = parser.add_argument_group('trainer')
        group.add_argument('--epochs', type=int,
                           help='number of epochs to train')
        group.add_argument('--train-batches', default=None, type=int,
                           help='number of train batches')
        group.add_argument('--val-batches', default=None, type=int,
                           help='number of val batches')

        group.add_argument('--clip-grad-norm', default=cls.clip_grad_norm, type=float,
                           help='clip grad norm: specify largest change for single param')
        group.add_argument('--clip-grad-value', default=cls.clip_grad_value, type=float,
                           help='clip grad value: specify largest change for single param')
        group.add_argument('--log-interval', default=cls.log_interval, type=int,
                           help='log loss every n steps')
        group.add_argument('--val-interval', default=cls.val_interval, type=int,
                           help='validation run every n epochs')

        group.add_argument('--stride-apply', default=cls.stride_apply, type=int,
                           help='apply and reset gradients every n batches')
        assert not cls.fix_batch_norm
        group.add_argument('--fix-batch-norm',
                           default=False, const=True, type=int, nargs='?',
                           help='fix batch norm running statistics (optionally specify epoch)')
        group.add_argument('--ema', default=cls.ema_decay, type=float,
                           help='ema decay constant')
        group.add_argument('--profile', default=cls.train_profile,
                           help='enables profiling. specify path for chrome tracing file')

    @classmethod
    def configure(cls, args: argparse.Namespace):
        cls.epochs = args.epochs
        cls.n_train_batches = args.train_batches
        cls.n_val_batches = args.val_batches

        cls.clip_grad_norm = args.clip_grad_norm
        cls.clip_grad_value = args.clip_grad_value
        cls.log_interval = args.log_interval
        cls.val_interval = args.val_interval

        cls.fix_batch_norm = args.fix_batch_norm
        cls.stride_apply = args.stride_apply
        cls.ema_decay = args.ema
        cls.train_profile = args.profile

    def lr(self):
        for param_group in self.optimizer.param_groups:
            return param_group['lr']

    def step_ema(self):
        if self.ema is None:
            return

        for p, ema_p in zip(self.model.parameters(), self.ema):
            ema_p.mul_(1.0 - self.ema_decay).add_(p.data, alpha=self.ema_decay)

    def apply_ema(self):
        if self.ema is None:
            return

        LOG.info('applying ema')
        self.ema_restore_params = copy.deepcopy(
            [p.data for p in self.model.parameters()])
        for p, ema_p in zip(self.model.parameters(), self.ema):
            p.data.copy_(ema_p)

    def ema_restore(self):
        if self.ema_restore_params is None:
            return

        LOG.info('restoring params from before ema')
        for p, ema_p in zip(self.model.parameters(), self.ema_restore_params):
            p.data.copy_(ema_p)
        self.ema_restore_params = None

    def loop(self,
             train_scenes: torch.utils.data.DataLoader,
             val_scenes: torch.utils.data.DataLoader,
             start_epoch=0):
        if start_epoch >= self.epochs:
            raise Exception('start epoch ({}) >= total epochs ({})'
                            ''.format(start_epoch, self.epochs))

        if self.lr_scheduler is not None:
            assert self.lr_scheduler.last_epoch == start_epoch * len(train_scenes)

        for epoch in range(start_epoch, self.epochs):
            if epoch == 0:
                self.write_model(0, final=False)
            if hasattr(train_scenes.sampler, 'set_epoch'):
                train_scenes.sampler.set_epoch(epoch)
            if hasattr(val_scenes.sampler, 'set_epoch'):
                val_scenes.sampler.set_epoch(epoch)

            #print("train_scenes")
            #print(len(train_scenes))
            #print("epoch")
            #print(epoch)
            #print("")
            self.train(train_scenes, epoch)
            
            if (epoch + 1) % self.val_interval == 0 \
               or epoch + 1 == self.epochs:
                self.write_model(epoch + 1, epoch + 1 == self.epochs)
                self.val(val_scenes, epoch + 1)

    # pylint: disable=method-hidden,too-many-branches,too-many-statements
    def train_batch(self, data, targets, apply_gradients=True):
        if self.device.type != 'cpu':
            assert data.is_pinned(), 'input data must be pinned'
            if targets[0] is not None:
                assert targets[0].is_pinned(), 'input targets must be pinned'
            with torch.autograd.profiler.record_function('to-device'):
                data = data.to(self.device, non_blocking=True)
                targets = [head.to(self.device, non_blocking=True)
                           if head is not None else None
                           for head in targets]

        # train encoder
        with torch.autograd.profiler.record_function('model'):
            outputs = self.model(data, head_mask=[t is not None for t in targets])
            #print("data")
            #print(data.size())
            #print(len(data[1][0][0]))
            #print("model")
            #print(type(self.model))
            #print(self.model)
            #print("head masks")
            #print([t is not None for t in targets[1][0][0]])
            if self.train_profile and self.device.type != 'cpu':
                torch.cuda.synchronize()
        with torch.autograd.profiler.record_function('loss'):
            #from this i know the issue is in outputs and in the targets, ehre tho ?
            #print("in trainer")
            #print("printing self.loss")
            #print("printing outputs")
            #print(len(outputs[1][0][0]))
            #print("printing targets")
            #print(len(targets[1][0][0]))
            #print(self.loss(outputs,targets))
            #print("")
            loss, head_losses = self.loss(outputs, targets)
            #print("self loss")
            #print(self.loss(outputs, targets))
            ###############################################################################################################################################################
            #This is where I should really change the losses right ? To have it fully accounted for in the backward propagation ?

            ###############################################################################################################################################################
            
            if self.train_profile and self.device.type != 'cpu':
                torch.cuda.synchronize()
        if loss is not None:
            with torch.autograd.profiler.record_function('backward'):
                loss.backward()
                if self.train_profile and self.device.type != 'cpu':
                    torch.cuda.synchronize()
        if self.clip_grad_norm:
            with torch.autograd.profiler.record_function('clip-grad-norm'):
                max_norm = self.clip_grad_norm / self.lr()
                total_norm = torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(), max_norm, norm_type=float('inf'))
                self.max_norm = max(float(total_norm), self.max_norm)
                if total_norm > max_norm:
                    self.n_clipped_grad += 1
                    print('CLIPPED GRAD NORM: total norm before clip: {}, max norm: {}'
                          ''.format(total_norm, max_norm))
                if self.train_profile and self.device.type != 'cpu':
                    torch.cuda.synchronize()
        if self.clip_grad_value:
            with torch.autograd.profiler.record_function('clip-grad-value'):
                torch.nn.utils.clip_grad_value_(self.model.parameters(), self.clip_grad_value)
                if self.train_profile and self.device.type != 'cpu':
                    torch.cuda.synchronize()
        if apply_gradients:
            with torch.autograd.profiler.record_function('step'):
                self.optimizer.step()
                self.optimizer.zero_grad()
                if self.train_profile and self.device.type != 'cpu':
                    torch.cuda.synchronize()
            with torch.autograd.profiler.record_function('ema'):
                self.step_ema()
                if self.train_profile and self.device.type != 'cpu':
                    torch.cuda.synchronize()

        with torch.inference_mode():
            with torch.autograd.profiler.record_function('reduce-losses'):
                loss = self.reduce_loss(loss)
                head_losses = self.reduce_loss(head_losses)
                if self.train_profile and self.device.type != 'cpu':
                    torch.cuda.synchronize()

        return (
            float(loss.item()) if loss is not None else None,
            [float(l.item()) if l is not None else None
             for l in head_losses],
        )

    @classmethod
    def reduce_loss(cls, loss):
        if not cls.distributed_reduce_loss:
            return loss
        if loss is None:
            return loss
        if not torch.distributed.is_initialized():
            return loss

        if isinstance(loss, (list, tuple)):
            return [cls.reduce_loss(l) for l in loss]

        # average loss from all processes
        torch.distributed.reduce(loss, 0)
        if torch.distributed.get_rank() == 0:
            loss = loss / torch.distributed.get_world_size()
        return loss

    def val_batch(self, data, targets):
        if self.device:
            data = data.to(self.device, non_blocking=True)
            targets = [head.to(self.device, non_blocking=True)
                       if head is not None else None
                       for head in targets]

        with torch.inference_mode():
            outputs = self.model(data)
            loss, head_losses = self.loss(outputs, targets)
            loss = self.reduce_loss(loss)
            head_losses = self.reduce_loss(head_losses)

        return (
            float(loss.item()) if loss is not None else None,
            [float(l.item()) if l is not None else None
             for l in head_losses],
        )

    # pylint: disable=too-many-branches
    def train(self, scenes, epoch):
        start_time = time.time()
        self.model.train()
        if self.fix_batch_norm is True \
           or (self.fix_batch_norm is not False and self.fix_batch_norm <= epoch):
            LOG.info('fix batchnorm')
            for m in self.model.modules():
                if isinstance(m, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d)):
                    LOG.debug('eval mode for: %s', m)
                    m.eval()

        self.ema_restore()
        self.ema = None

        epoch_loss = 0.0
        head_epoch_losses = None
        head_epoch_counts = None
        last_batch_end = time.time()
        self.optimizer.zero_grad()
        #problem is coming from the scenes
        #print("in train fct")
        #print("scenes")
        #print(type(scenes))
        #print(scenes)
        for batch_idx, (data, target, _) in enumerate(scenes):
            #print("printing what in scenes")
            #print("batch_idx")
            #print(batch_idx)
            #print("target")
            #print(target)
            preprocess_time = time.time() - last_batch_end

            batch_start = time.time()
            apply_gradients = batch_idx % self.stride_apply == 0
            #print("in batch idx")
            #print("printing targets")
            #print(len(target[1][0][0]))
            loss, head_losses = self.train_batch(data, target, apply_gradients)
            #print("head losses")
            #print(type(head_losses))
            #print(len(head_losses))

            ########################################################################################################################################################################
            #Here is where I have access to losses of this batch, do I do DWA on every batch ?
            self.head_loss_array.append(head_losses)
            lambda_head_indexing = []
            new_loss = 0
            size_batch = len(data)
            #print("appended head losses")
            #print(self.head_loss_array)

            if self.start_epoch == -1:
                print("first epoch")
                print(epoch)
                self.start_epoch = epoch
            #print("epoch")
            #print(epoch)
            #print(self.start_epoch)
            #Implementing Dynamic Weight Average
            if epoch <= self.start_epoch+2:
                pass
            else:
                #print("in DWA")
                prevWeight = [a/b for a,b in zip(self.head_loss_array[epoch-self.start_epoch-1],self.head_loss_array[epoch-self.start_epoch-2])]
                a = [math.exp(x/self.T) for x in prevWeight]
                b = sum(a) 
                lambda_head_indexing = [self.K*x/b for x in a]
                #print("lambda head indexing ")
                #print(lambda_head_indexing)
                #print("prevWeight")
                #print(prevWeight)
            if len(lambda_head_indexing):
                new_loss = sum([a*b for a,b in zip(lambda_head_indexing,head_losses)])
                self.avg_loss[epoch-self.start_epoch]+= head_losses/size_batch
                '''
                print("")
                print("")
                print("")
                print("New loss")
                print(new_loss)
                print("old loss")
                print(loss)
                print("new head losses")
                print([a*b for a,b in zip(lambda_head_indexing,head_losses)])
                print("hold head losses")
                print(head_losses)
                print("")
                print("")
                print("")
                '''
            ########################################################################################################################################################################

            # update epoch accumulates
            if loss is not None:
                if new_loss:
                    print("using new loss")
                    #epoch_loss += new_loss
                    #loss = new_loss
                    epoch_loss += loss
                else:
                    epoch_loss += loss
            if head_epoch_losses is None:
                head_epoch_losses = [0.0 for _ in head_losses]
                head_epoch_counts = [0 for _ in head_losses]
            for i, head_loss in enumerate(head_losses):
                if head_loss is None:
                    continue
                head_epoch_losses[i] += head_loss
                head_epoch_counts[i] += 1

            batch_time = time.time() - batch_start

            # write training loss
            if batch_idx % self.log_interval == 0:
                batch_info = {
                    'type': 'train',
                    'epoch': epoch, 'batch': batch_idx, 'n_batches': len(scenes),
                    'time': round(batch_time, 3),
                    'data_time': round(preprocess_time, 3),
                    'lr': round(self.lr(), 8),
                    'loss': round(loss, 3) if loss is not None else None,
                    'head_losses': [round(l, 3) if l is not None else None
                                    for l in head_losses],
                }
                if hasattr(self.loss, 'batch_meta'):
                    batch_info.update(self.loss.batch_meta())
                LOG.info(batch_info)

            # initialize ema
            if self.ema is None and self.ema_decay:
                self.ema = copy.deepcopy([p.data for p in self.model.parameters()])

            # update learning rate
            if self.lr_scheduler is not None:
                self.lr_scheduler.step()

            if self.n_train_batches and batch_idx + 1 >= self.n_train_batches:
                break

            last_batch_end = time.time()

        self.apply_ema()
        LOG.info({
            'type': 'train-epoch',
            'epoch': epoch + 1,
            'loss': round(epoch_loss / len(scenes), 5),
            'head_losses': [round(l / max(1, c), 5)
                            for l, c in zip(head_epoch_losses, head_epoch_counts)],
            'time': round(time.time() - start_time, 1),
            'n_clipped_grad': self.n_clipped_grad,
            'max_norm': self.max_norm,
        })
        self.n_clipped_grad = 0
        self.max_norm = 0.0

    def val(self, scenes, epoch):
        start_time = time.time()

        # Train mode implies outputs are for losses, so have to use it here.
        self.model.train()
        if self.fix_batch_norm is True \
           or (self.fix_batch_norm is not False and self.fix_batch_norm <= epoch - 1):
            LOG.info('fix batchnorm')
            for m in self.model.modules():
                if isinstance(m, (torch.nn.BatchNorm1d, torch.nn.BatchNorm2d)):
                    LOG.debug('eval mode for: %s', m)
                    m.eval()

        epoch_loss = 0.0
        head_epoch_losses = None
        head_epoch_counts = None
        for batch_idx, (data, target, _) in enumerate(scenes):
            loss, head_losses = self.val_batch(data, target)

            # update epoch accumulates
            if loss is not None:
                epoch_loss += loss
            if head_epoch_losses is None:
                head_epoch_losses = [0.0 for _ in head_losses]
                head_epoch_counts = [0 for _ in head_losses]
            for i, head_loss in enumerate(head_losses):
                if head_loss is None:
                    continue
                head_epoch_losses[i] += head_loss
                head_epoch_counts[i] += 1

            if self.n_val_batches and batch_idx + 1 >= self.n_val_batches:
                break

        eval_time = time.time() - start_time

        LOG.info({
            'type': 'val-epoch',
            'epoch': epoch,
            'loss': round(epoch_loss / len(scenes), 5),
            'head_losses': [round(l / max(1, c), 5)
                            for l, c in zip(head_epoch_losses, head_epoch_counts)],
            'time': round(eval_time, 1),
        })

    def write_model(self, epoch, final=True):
        if torch.distributed.is_initialized() and torch.distributed.get_rank() != 0:
            return

        model_to_save = self.model
        if self.checkpoint_shell is not None:
            model = self.model if not hasattr(self.model, 'module') else self.model.module
            self.checkpoint_shell.load_state_dict(model.state_dict())
            model_to_save = self.checkpoint_shell

        filename = '{}.epoch{:03d}'.format(self.out, epoch)
        LOG.debug('about to write model')
        torch.save({
            'model': model_to_save,
            'epoch': epoch,
            'meta': self.model_meta_data,
        }, filename)
        LOG.info('model written: %s', filename)

        optim_filename = '{}.optim.epoch{:03d}'.format(self.out, epoch)
        LOG.debug('about to write training state')
        torch.save({
            'optimizer': self.optimizer.state_dict(),
            'loss': self.loss.state_dict(),
        }, optim_filename)
        LOG.info('training state written: %s', optim_filename)

        if final:
            sha256_hash = hashlib.sha256()
            with open(filename, 'rb') as f:
                for byte_block in iter(lambda: f.read(8192), b''):
                    sha256_hash.update(byte_block)
            file_hash = sha256_hash.hexdigest()
            outname, _, outext = self.out.rpartition('.')
            final_filename = '{}-{}.{}'.format(outname, file_hash[:8], outext)
            shutil.copyfile(filename, final_filename)
