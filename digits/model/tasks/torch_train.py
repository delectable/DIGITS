# Copyright (c) 2014-2015, NVIDIA CORPORATION.  All rights reserved.

import os
import re
import caffe
import time
import math
import subprocess
import sys
import operator

import numpy as np

import h5py

import tempfile
import PIL.Image
import digits
from train import TrainTask
from digits.config import config_value
from digits.status import Status
from digits import utils, dataset
from digits.utils import subclass, override, constants, errors
from digits.dataset import ImageClassificationDatasetJob

# NOTE: Increment this everytime the pickled object changes
PICKLE_VERSION = 1

@subclass
class TorchTrainTask(TrainTask):
    """
    Trains a torch model
    """

    TORCH_LOG = 'torch_output.log'

    def __init__(self, shuffle, **kwargs):
        """
        Arguments:
        network -- a NetParameter defining the network
        """
        super(TorchTrainTask, self).__init__(**kwargs)
        self.pickver_task_torch_train = PICKLE_VERSION

        self.shuffle = shuffle

        self.current_epoch = 0

        self.loaded_snapshot_file = None
        self.loaded_snapshot_epoch = None
        self.image_mean = None
        self.classifier = None
        self.solver = None

        self.model_file = constants.TORCH_MODEL_FILE
        self.train_file = constants.TRAIN_DB
        self.val_file = constants.VAL_DB
        self.snapshot_prefix = constants.TORCH_SNAPSHOT_PREFIX
        self.log_file = self.TORCH_LOG

    def __getstate__(self):
        state = super(TorchTrainTask, self).__getstate__()

        # Don't pickle these things
        if 'labels' in state:
            del state['labels']
        if 'image_mean' in state:
            del state['image_mean']
        if 'classifier' in state:
            del state['classifier']
        if 'torch_log' in state:
            del state['torch_log']

        return state

    def __setstate__(self, state):
        super(TorchTrainTask, self).__setstate__(state)

        # Make changes to self
        self.loaded_snapshot_file = None
        self.loaded_snapshot_epoch = None

        # These things don't get pickled
        self.image_mean = None
        self.classifier = None

    ### Task overrides

    @override
    def name(self):
        return 'Train Torch Model'

    @override
    def framework_name(self):
        return 'torch'

    @override
    def before_run(self):
        if not isinstance(self.dataset, dataset.ImageClassificationDatasetJob):
            raise NotImplementedError()

        self.torch_log = open(self.path(self.TORCH_LOG), 'a')
        self.saving_snapshot = False
        self.receiving_train_output = False
        self.receiving_val_output = False
        self.last_train_update = None
        self.displaying_network = False
        self.temp_unrecognized_output = []
        return True

    @override
    def task_arguments(self, resources):
        if config_value('torch_root') == '<PATHS>':
            torch_bin = 'th'
        else:
            torch_bin = os.path.join(config_value('torch_root'), 'bin', 'th')

        if self.batch_size is None:
            self.batch_size = constants.DEFAULT_TORCH_BATCH_SIZE

        args = [torch_bin,
                os.path.join(os.path.dirname(os.path.dirname(digits.__file__)),'tools','torch','main.lua'),
                '--network=%s' % self.model_file.split(".")[0],
                '--epoch=%d' % int(self.train_epochs),
                '--train=%s' % self.dataset.path(constants.TRAIN_DB),
                '--networkDirectory=%s' % self.job_dir,
                '--save=%s' % self.job_dir,
                '--snapshotPrefix=%s' % self.snapshot_prefix,
                '--snapshotInterval=%s' % self.snapshot_interval,
                '--useMeanPixel=yes',
                '--mean=%s' % self.dataset.path(constants.MEAN_FILE_IMAGE),
                '--labels=%s' % self.dataset.path(self.dataset.labels_file),
                '--batchSize=%d' % self.batch_size,
                '--learningRate=%s' % self.learning_rate,
                '--policy=%s' % str(self.lr_policy['policy'])
                ]

        #learning rate policy input parameters
        if self.lr_policy['policy'] == 'fixed':
            pass
        elif self.lr_policy['policy'] == 'step':
            args.append('--gamma=%s' % self.lr_policy['gamma'])
            args.append('--stepvalues=%s' % self.lr_policy['stepsize'])
        elif self.lr_policy['policy'] == 'multistep':
            args.append('--stepvalues=%s' % self.lr_policy['stepvalue'])
            args.append('--gamma=%s' % self.lr_policy['gamma'])
        elif self.lr_policy['policy'] == 'exp':
            args.append('--gamma=%s' % self.lr_policy['gamma'])
        elif self.lr_policy['policy'] == 'inv':
            args.append('--gamma=%s' % self.lr_policy['gamma'])
            args.append('--power=%s' % self.lr_policy['power'])
        elif self.lr_policy['policy'] == 'poly':
            args.append('--power=%s' % self.lr_policy['power'])
        elif self.lr_policy['policy'] == 'sigmoid':
            args.append('--stepvalues=%s' % self.lr_policy['stepsize'])
            args.append('--gamma=%s' % self.lr_policy['gamma'])

        if self.shuffle:
            args.append('--shuffle=yes')

        if self.crop_size:
            args.append('--crop=yes')
            args.append('--croplen=%d' % self.crop_size)

        if self.use_mean:
            args.append('--subtractMean=yes')
        else:
            args.append('--subtractMean=no')

        if self.random_seed is not None:
            args.append('--seed=%s' % self.random_seed)

        if self.solver_type == 'NESTEROV':
            args.append('--optimization=nag')

        if self.solver_type == 'ADAGRAD':
            args.append('--optimization=adagrad')

        if self.solver_type == 'SGD':
            args.append('--optimization=sgd')

        if os.path.exists(self.dataset.path(constants.VAL_DB)) and self.val_interval > 0:
            args.append('--validation=%s' % self.dataset.path(constants.VAL_DB))
            args.append('--interval=%s' % self.val_interval)

        if 'gpus' in resources:
            identifiers = []
            for identifier, value in resources['gpus']:
                identifiers.append(int(identifier))
            if len(identifiers) == 1:
                args.append('--devid=%s' % (identifiers[0]+1,))
            elif len(identifiers) > 1:
                raise NotImplementedError("haven't tested torch with multiple GPUs yet")

        if self.pretrained_model:
            args.append('--weights=%s' % self.path(self.pretrained_model))

        return args

    @override
    def process_output(self, line):
        from digits.webapp import socketio
        regex = re.compile('\x1b\[[0-9;]*m', re.UNICODE)   #TODO: need to include regular expression for MAC color codes
        line=regex.sub('', line).strip()
        self.torch_log.write('%s\n' % line)
        self.torch_log.flush()

        # parse torch output
        timestamp, level, message = self.preprocess_output_torch(line)

        # return false when unrecognized output is encountered
        if not level:
            # network display in progress
            if self.displaying_network:
                self.temp_unrecognized_output.append(line)
                return True
            return False

        if not message:
            return True

        # network display ends
        if self.displaying_network:
            if message.startswith('Network definition ends'):
                self.temp_unrecognized_output = []
                self.displaying_network = False
            return True

        float_exp = '([-]?inf|[-+]?[0-9]*\.?[0-9]+(e[-+]?[0-9]+)?)'

        # loss and learning rate updates
        match = re.match(r'Training \(epoch (\d+\.?\d*)\): \w*loss\w* = %s, lr = %s'  % (float_exp, float_exp), message)
        if match:
            index = float(match.group(1))
            l = match.group(2)
            assert l.lower() != '-inf', 'Network reported -inf for training loss. Try changing your learning rate.'       #TODO: messages needs to be corrected
            assert l.lower() != 'inf', 'Network reported inf for training loss. Try decreasing your learning rate.'
            l = float(l)
            lr = match.group(4)
            assert lr.lower() != '-inf', 'Network reported -inf for learning rate. Try changing your learning rate.'
            assert lr.lower() != 'inf', 'Network reported inf for learning rate. Try decreasing your learning rate.'
            lr = float(lr)
            # epoch updates
            self.send_progress_update(index)

            self.save_train_output('loss', 'SoftmaxWithLoss', l)
            self.save_train_output('learning_rate', 'LearningRate', lr)
            self.logger.debug(message)

            return True

        # testing loss and accuracy updates
        match = re.match(r'Validation \(epoch (\d+\.?\d*)\): \w*loss\w* = %s, accuracy = %s' % (float_exp,float_exp), message, flags=re.IGNORECASE)
        if match:
            index = float(match.group(1))
            l = match.group(2)
            a = match.group(4)
            if l.lower() != 'inf' and l.lower() != '-inf' and a.lower() != 'inf' and a.lower() != '-inf':
                l = float(l)
                a = float(a)
                self.logger.debug('Network accuracy #%s: %s' % (index, a))
                # epoch updates
                self.send_progress_update(index)

                self.save_val_output('accuracy', 'Accuracy', a)
                self.save_val_output('loss', 'SoftmaxWithLoss', l)

            return True

        # snapshot saved
        if self.saving_snapshot:
            if not message.startswith('Snapshot saved'):
                self.logger.warning('Torch output format seems to have changed. Expected "Snapshot saved..." after "Snapshotting to..."')
            else:
                self.logger.info('Snapshot saved.')  # to print file name here, you can use "message"
            self.detect_snapshots()
            self.send_snapshot_update()
            self.saving_snapshot = False
            return True

        # snapshot starting
        match = re.match(r'Snapshotting to (.*)\s*$', message)
        if match:
            self.saving_snapshot = True
            return True

        # network display starting
        if message.startswith('Network definition:'):
            self.displaying_network = True
            return True

        if level in ['error', 'critical']:
            self.logger.error('%s: %s' % (self.name(), message))
            self.exception = message
            return True

        # skip remaining info and warn messages
        return True

    def preprocess_output_torch(self, line):
        """
        Takes line of output and parses it according to caffe's output format
        Returns (timestamp, level, message) or (None, None, None)
        """
        # NOTE: This must change when the logging format changes
        # LMMDD HH:MM:SS.MICROS pid file:lineno] message
        match = re.match(r'(\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}:\d{2})\s\[(\w+)\s*]\s+(\S.*)$', line)
        if match:
            timestamp = time.mktime(time.strptime(match.group(1), '%Y-%m-%d %H:%M:%S'))
            level = match.group(2)
            message = match.group(3)
            if level == 'INFO':
                level = 'info'
            elif level == 'WARNING':
                level = 'warning'
            elif level == 'ERROR':
                level = 'error'
            elif level == 'FAIL': #FAIL
                level = 'critical'
            return (timestamp, level, message)
        else:
            #self.logger.warning('Unrecognized task output "%s"' % line)
            return (None, None, None)

    def send_snapshot_update(self):
        """
        Sends socketio message about the snapshot list
        """
        # TODO: move to TrainTask
        from digits.webapp import socketio

        socketio.emit('task update',
                {
                    'task': self.html_id(),
                    'update': 'snapshots',
                    'data': self.snapshot_list(),
                    },
                namespace='/jobs',
                room=self.job_id,
                )

    ### TrainTask overrides
    @override
    def after_run(self):
        if self.temp_unrecognized_output:
            if self.traceback:
                self.traceback = self.traceback + ('\n'.join(self.temp_unrecognized_output))
            else:
                self.traceback = '\n'.join(self.temp_unrecognized_output)
                self.temp_unrecognized_output = []
        self.torch_log.close()

    @override
    def after_runtime_error(self):
        if os.path.exists(self.path(self.TORCH_LOG)):
            output = subprocess.check_output(['tail', '-n40', self.path(self.TORCH_LOG)])
            lines = []
            for line in output.split('\n'):
                # parse torch header
                timestamp, level, message = self.preprocess_output_torch(line)

                if message:
                    lines.append(message)
            # return the last 20 lines
            self.traceback = '\n'.join(lines[len(lines)-20:])

    @override
    def detect_snapshots(self):
        self.snapshots = []

        snapshot_dir = os.path.join(self.job_dir, os.path.dirname(self.snapshot_prefix))
        snapshots = []
        solverstates = []

        for filename in os.listdir(snapshot_dir):
            # find models
            match = re.match(r'%s_(\d+)\.?(\d*)_Weights\.t7' % os.path.basename(self.snapshot_prefix), filename)
            if match:
                epoch = 0
                if match.group(2) == '':
                    epoch = int(match.group(1))
                else:
                    epoch = float(match.group(1) + '.' + match.group(2))
                snapshots.append( (
                        os.path.join(snapshot_dir, filename),
                        epoch
                        )
                    )

        self.snapshots = sorted(snapshots, key=lambda tup: tup[1])

        return len(self.snapshots) > 0

    @override
    def est_next_snapshot(self):
        # TODO: Currently this function is not in use. Probably in future we may have to implement this
        return None

    @override
    def can_view_weights(self):
        return False

    @override
    def can_infer_one(self):
        if isinstance(self.dataset, ImageClassificationDatasetJob):
            return True
        return False

    @override
    def infer_one(self, data, snapshot_epoch=None, layers=None):
        if isinstance(self.dataset, ImageClassificationDatasetJob):
            return self.classify_one(data,
                    snapshot_epoch=snapshot_epoch,
                    layers=layers,
                    )
        raise NotImplementedError()

    def classify_one(self, image, snapshot_epoch=None, layers=None):
        """
        Classify an image
        Returns (predictions, visualizations)
            predictions -- an array of [ (label, confidence), ...] for each label, sorted by confidence
            visualizations -- an array of (layer_name, activations, weights) for the specified layers
        Returns (None, None) if something goes wrong

        Arguments:
        image -- a np.array

        Keyword arguments:
        snapshot_epoch -- which snapshot to use
        layers -- which layer activation[s] and weight[s] to visualize
        """
        _, temp_image_path = tempfile.mkstemp(suffix='.jpeg')
        image = PIL.Image.fromarray(image)
        try:
            image.save(temp_image_path, format='jpeg')
        except KeyError:
            error_message = 'Unable to save file to "%s"' % temp_image_path
            self.logger.error(error_message)
            raise errors.TestError(error_message)

        if config_value('torch_root') == '<PATHS>':
            torch_bin = 'th'
        else:
            torch_bin = os.path.join(config_value('torch_root'), 'bin', 'th')

        args = [torch_bin,
                os.path.join(os.path.dirname(os.path.dirname(digits.__file__)),'tools','torch','test.lua'),
		        '--image=%s' % temp_image_path,
                '--network=%s' % self.model_file.split(".")[0],
                '--networkDirectory=%s' % self.job_dir,
                '--load=%s' % self.job_dir,
                '--snapshotPrefix=%s' % self.snapshot_prefix,
                '--mean=%s' % self.dataset.path(constants.MEAN_FILE_IMAGE),
                '--labels=%s' % self.dataset.path(self.dataset.labels_file)
                ]
        if snapshot_epoch:
            args.append('--epoch=%d' % int(snapshot_epoch))
        if constants.TORCH_USE_MEAN_PIXEL:
            args.append('--useMeanPixel=yes')

        if self.crop_size:
            args.append('--crop=yes')
            args.append('--croplen=%d' % self.crop_size)

        if self.use_mean:
            args.append('--subtractMean=yes')
        else:
            args.append('--subtractMean=no')

        if layers=='all':
            args.append('--visualization=yes')
            args.append('--save=%s' % self.job_dir)

        # Convert them all to strings
        args = [str(x) for x in args]

        regex = re.compile('\x1b\[[0-9;]*m', re.UNICODE)   #TODO: need to include regular expression for MAC color codes
        self.logger.info('%s classify one task started.' % self.framework_name())

        unrecognized_output = []
        predictions = []
        self.visualization_file = None

        p = subprocess.Popen(args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.job_dir,
                close_fds=True,
                )

        try:
            while p.poll() is None:
                for line in utils.nonblocking_readlines(p.stdout):
                    if self.aborted.is_set():
                        p.terminate()
                        raise errors.TestError('%s classify one task got aborted. error code - %d' % (self.framework_name(), p.returncode()))

                    if line is not None:
                        # Remove color codes and whitespace
                        line=regex.sub('', line).strip()
                    if line:
                        if not self.process_test_output(line, predictions, 'one'):
                            self.logger.warning('%s classify one task unrecognized input: %s' % (self.framework_name(), line.strip()))
                            unrecognized_output.append(line)
                    else:
                        time.sleep(0.05)

        except Exception as e:
            if p.poll() is None:
                p.terminate()
            error_message = ''
            if type(e) == errors.TestError:
                error_message = e.__str__()
            else:
                error_message = '%s classify one task failed with error code %d \n %s' % (self.framework_name(), p.returncode(), str(e))
            self.logger.error(error_message)
            if unrecognized_output:
                unrecognized_output = '\n'.join(unrecognized_output)
                error_message = error_message + unrecognized_output
            raise errors.TestError(error_message)

        finally:
            self.after_test_run(temp_image_path)

        if p.returncode != 0:
            error_message = '%s classify one task failed with error code %d' % (self.framework_name(), p.returncode)
            self.logger.error(error_message)
            if unrecognized_output:
                unrecognized_output = '\n'.join(unrecognized_output)
                error_message = error_message + unrecognized_output
            raise errors.TestError(error_message)
        else:
            self.logger.info('%s classify one task completed.' % self.framework_name())


        visualizations = []

        if layers=='all' and self.visualization_file:
            vis_db = h5py.File(self.visualization_file, 'r')
            # the HDF5 database is organized as follows:
            # <root>
            # |- layers
            #    |- 1
            #    |  |- name
            #    |  |- activations
            #    |  |- weights
            #    |- 2
            for layer_id,layer in vis_db['layers'].items():
                layer_desc = layer['name'][...].tostring()
                if 'Sequential' in layer_desc or 'Parallel' in layer_desc:
                    # ignore containers
                    continue
                idx = int(layer_id)
                # activations
                data = np.array(layer['activations'][...][0])
                vis = self.get_layer_vis_square(data)
                mean, std, hist = self.get_layer_statistics(data)
                visualizations.append(
                                         {
                                             'id':         idx,
                                             'name':       layer_desc,
                                             'vis_type':   'Activations',
                                             'image_html': utils.image.embed_image_html(vis),
                                             'data_stats': {
                                                              'shape':      data.shape,
                                                              'mean':       mean,
                                                              'stddev':     std,
                                                              'histogram':  hist,
                                             }
                                         }
                                     )
                # weights
                if 'weights' in layer:
                    data = np.array(layer['weights'][...])
                    if 'Linear' not in layer_desc:
                        vis = self.get_layer_vis_square(data)
                    else:
                        # Linear (inner product) layers have too many weights
                        # to display
                        vis = None
                    mean, std, hist = self.get_layer_statistics(data)
                    parameter_count = reduce(operator.mul, data.shape, 1)
                    if 'bias' in layer:
                        bias = np.array(layer['bias'][...])
                        parameter_count += reduce(operator.mul, bias.shape, 1)
                    visualizations.append(
                                           {
                                               'id':          idx,
                                               'name':        layer_desc,
                                               'vis_type':    'Weights',
                                               'image_html':  utils.image.embed_image_html(vis),
                                               'param_count': parameter_count,
                                               'data_stats': {
                                                                 'shape':      data.shape,
                                                                 'mean':       mean,
                                                                 'stddev':     std,
                                                                 'histogram':  hist,
                                               }
                                           }
                                         )
            # sort by layer ID
            visualizations = sorted(visualizations,key=lambda x:x['id'])
        return (predictions,visualizations)

    def get_layer_vis_square(self, data,
            allow_heatmap = True,
            normalize = True,
            max_width = 1200,
            ):
        """
        Returns a vis_square for the given layer data

        Arguments:
        data -- a np.ndarray

        Keyword arguments:
        allow_heatmap -- if True, convert single channel images to heatmaps
        normalize -- whether to normalize the data when visualizing
        max_width -- maximum width for the vis_square
        """
        if data.ndim == 1:
            # interpret as 1x1 grayscale images
            # (N, 1, 1)
            data = data[:, np.newaxis, np.newaxis]
        elif data.ndim == 2:
            # interpret as 1x1 grayscale images
            # (N, 1, 1)
            data = data.reshape((data.shape[0]*data.shape[1], 1, 1))
        elif data.ndim == 3:
            if data.shape[0] == 3:
                # interpret as a color image
                # (1, H, W,3)
                data = data[[2,1,0],...] # BGR to RGB (see issue #59)
                data = data.transpose(1,2,0)
                data = data[np.newaxis,...]
            else:
                # interpret as grayscale images
                # (N, H, W)
                pass
        elif data.ndim == 4:
            if data.shape[0] == 3:
                # interpret as HxW color images
                # (N, H, W, 3)
                data = data.transpose(1,2,3,0)
                data = data[:,:,:,[2,1,0]] # BGR to RGB (see issue #59)
            elif data.shape[1] == 3:
                # interpret as HxW color images
                # (N, H, W, 3)
                data = data.transpose(0,2,3,1)
                data = data[:,:,:,[2,1,0]] # BGR to RGB (see issue #59)
            else:
                # interpret as HxW grayscale images
                # (N, H, W)
                data = data.reshape((data.shape[0]*data.shape[1], data.shape[2], data.shape[3]))
        else:
            raise RuntimeError('unrecognized data shape: %s' % (data.shape,))

        # chop off data so that it will fit within max_width
        padsize = 0
        width = data.shape[2]
        if width > max_width:
            data = data[:1,:max_width,:max_width]
        else:
            if width > 1:
                padsize = 1
                width += 1
            n = max(max_width/width,1)
            n *= n
            data = data[:n]

        if not allow_heatmap and data.ndim == 3:
            data = data[...,np.newaxis]

        return utils.image.vis_square(data,
                padsize     = padsize,
                normalize   = normalize,
                )


    def get_layer_statistics(self, data):
        """
        Returns statistics for the given layer data:
            (mean, standard deviation, histogram)
                histogram -- [y, x, ticks]

        Arguments:
        data -- a np.ndarray
        """
        # XXX These calculations can be super slow
        mean = np.mean(data)
        std = np.std(data)
        y, x = np.histogram(data, bins=20)
        y = list(y)
        ticks = x[[0,len(x)/2,-1]]
        x = [(x[i]+x[i+1])/2.0 for i in xrange(len(x)-1)]
        ticks = list(ticks)
        return (mean, std, [y, x, ticks])


    def after_test_run(self, temp_image_path):
        try:
            os.remove(temp_image_path)
        except OSError:
            pass

    def process_test_output(self, line, predictions, test_category):
        #from digits.webapp import socketio

        # parse torch output
        timestamp, level, message = self.preprocess_output_torch(line)

        # return false when unrecognized output is encountered
        if not (level or message):
            return False

        if not message:
            return True

        float_exp = '([-]?inf|[-+]?[0-9]*\.?[0-9]+(e[-+]?[0-9]+)?)'

        # format of output while testing single image
        match = re.match(r'For image \d+, predicted class \d+: \d+ \((.*?)\) %s'  % (float_exp), message)
        if match:
            label = match.group(1)
            confidence = match.group(2)
            assert confidence.lower() != 'nan', 'Network reported "nan" for confidence value. Please check image and network'
            confidence = float(confidence)
            predictions.append((label, confidence))
            return True

        # format of output while testing multiple images
        match = re.match(r'Predictions for image \d+: (.*)', message)
        if match:
            values = match.group(1).strip().split(" ")
	    predictions.append(map(float, values))
            return True

        # path to visualization file
        match = re.match(r'Saving visualization to (.*)', message)
        if match:
            self.visualization_file = match.group(1).strip()
            return True

        # displaying info and warn messages as we aren't maintaining seperate log file for model testing
        if level == 'info':
            self.logger.debug('%s classify %s task : %s' % (self.framework_name(), test_category, message))
            return True
        if level == 'warning':
            self.logger.warning('%s classify %s task : %s' % (self.framework_name(), test_category, message))
            return True

        if level in ['error', 'critical']:
            raise errors.TestError('%s classify %s task failed with error message - %s' % (self.framework_name(), test_category, message))

        return True           # control never reach this line. It can be removed.

    @override
    def can_infer_many(self):
        if isinstance(self.dataset, ImageClassificationDatasetJob):
            return True
        raise NotImplementedError()

    @override
    def infer_many(self, data, snapshot_epoch=None):
        if isinstance(self.dataset, ImageClassificationDatasetJob):
            return self.classify_many(data, snapshot_epoch=snapshot_epoch)
        raise NotImplementedError()

    def classify_many(self, image_file, snapshot_epoch=None):
        """
        Returns (labels, results):
        labels -- an array of strings
        results -- a 2D np array:
            [
                [image0_label0_confidence, image0_label1_confidence, ...],
                [image1_label0_confidence, image1_label1_confidence, ...],
                ...
            ]

        Arguments:
        images -- a list of np.arrays

        Keyword arguments:
        snapshot_epoch -- which snapshot to use
        """
	labels = self.get_labels()         #TODO: probably we no need to return this, as we can directly access from the calling function

        if config_value('torch_root') == '<PATHS>':
            torch_bin = 'th'
        else:
            torch_bin = os.path.join(config_value('torch_root'), 'bin', 'th')

        args = [torch_bin,
                os.path.join(os.path.dirname(os.path.dirname(digits.__file__)),'tools','torch','test.lua'),
		'--testMany=yes',
		'--allPredictions=yes',   #all predictions are grabbed and formatted as required by DIGITS
		'--image=%s' % str(image_file),
                '--resizeMode=%s' % str(self.dataset.resize_mode),   # Here, we are using original images, so they will be resized in Torch code. This logic needs to be changed to eliminate the rework of resizing. Need to find a way to send python images array to Lua script efficiently
                '--network=%s' % self.model_file.split(".")[0],
                '--networkDirectory=%s' % self.job_dir,
                '--load=%s' % self.job_dir,
                '--snapshotPrefix=%s' % self.snapshot_prefix,
                '--mean=%s' % self.dataset.path(constants.MEAN_FILE_IMAGE),
		'--pythonPrefix=%s' % sys.executable,
                '--labels=%s' % self.dataset.path(self.dataset.labels_file)
                ]
        if snapshot_epoch:
            args.append('--epoch=%d' % int(snapshot_epoch))
        if constants.TORCH_USE_MEAN_PIXEL:
            args.append('--useMeanPixel=yes')

        if self.crop_size:
            args.append('--crop=yes')
            args.append('--croplen=%d' % self.crop_size)

        if self.use_mean:
            args.append('--subtractMean=yes')
        else:
            args.append('--subtractMean=no')

        #print args

        # Convert them all to strings
        args = [str(x) for x in args]

        regex = re.compile('\x1b\[[0-9;]*m', re.UNICODE)   #TODO: need to include regular expression for MAC color codes
        self.logger.info('%s classify many task started.' % self.name())

        unrecognized_output = []
	predictions = []
        p = subprocess.Popen(args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=self.job_dir,
                close_fds=True,
                )

        try:
            while p.poll() is None:
                for line in utils.nonblocking_readlines(p.stdout):
                    if self.aborted.is_set():
                        p.terminate()
                        raise errors.TestError('%s classify many task got aborted. error code - %d' % (self.framework_name(), p.returncode()))

                    if line is not None:
                        # Remove whitespace and color codes. color codes are appended to begining and end of line by torch binary i.e., 'th'. Check the below link for more information
                        # https://groups.google.com/forum/#!searchin/torch7/color$20codes/torch7/8O_0lSgSzuA/Ih6wYg9fgcwJ
                        line=regex.sub('', line).strip()
                    if line:
                        if not self.process_test_output(line, predictions, 'many'):
                            self.logger.warning('%s classify many task unrecognized input: %s' % (self.framework_name(), line.strip()))
                            unrecognized_output.append(line)
                    else:
                        time.sleep(0.05)
        except Exception as e:
            if p.poll() is None:
                p.terminate()
            error_message = ''
            if type(e) == errors.TestError:
                error_message = e.__str__()
            else:
                error_message = '%s classify many task failed with error code %d \n %s' % (self.framework_name(), p.returncode(), str(e))
            self.logger.error(error_message)
            if unrecognized_output:
                unrecognized_output = '\n'.join(unrecognized_output)
                error_message = error_message + unrecognized_output
            raise errors.TestError(error_message)

        if p.returncode != 0:
            error_message = '%s classify many task failed with error code %d' % (self.framework_name(), p.returncode)
            self.logger.error(error_message)
            if unrecognized_output:
                unrecognized_output = '\n'.join(unrecognized_output)
                error_message = error_message + unrecognized_output
            raise errors.TestError(error_message)
        else:
            self.logger.info('%s classify many task completed.' % self.framework_name())

	return (labels,np.array(predictions))

    def has_model(self):
        """
        Returns True if there is a model that can be used
        """
        return len(self.snapshots) != 0

    def loaded_model(self):
        """
        Returns True if a model has been loaded
        """
        return None

    def load_model(self, epoch=None):
        """
        Loads a .caffemodel
        Returns True if the model is loaded (or if it was already loaded)

        Keyword Arguments:
        epoch -- which snapshot to load (default is -1 to load the most recently generated snapshot)
        """
        return False

