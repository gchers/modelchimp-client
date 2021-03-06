from __future__ import print_function

import json
import zlib
import sys
import inspect
import os
import atexit
import queue
import logging
import pickle
import cloudpickle

from datetime import datetime
from modelchimp import settings

from . import metrics
from .sklearn_tracker import sklearn_loader
from .tracker_thread import TrackerThread
from .event_queue import event_queue
from .connection_thread import WSConnectionThread, RestConnection
from .utils import generate_uid, current_string_datetime, is_uuid4_pattern
from .enum import ClientEvent
from .log import get_logger

logger = get_logger(__name__)

class Tracker:
    def __init__(self,
                key,
                host=None,
                experiment_name=None,
                tracking=True,
                auto_log=False,
                existing_exp_id=None):
        self.key = key
        self.experiment_name = experiment_name
        self.host = host
        self.rest = None
        self.tracking = tracking
        self.auto_log = auto_log
        self._experiment_start = current_string_datetime()
        self._experiment_end = None
        self._experiment_file = None

        if existing_exp_id:
            self.experiment_id = existing_exp_id
        else:
            self.experiment_id = generate_uid()

        self._initialize()


    def _initialize(self):
        # Get the filename path of the script
        frame = inspect.stack()[2]
        module = inspect.getmodule(frame[0])
        self._experiment_file = module.__file__

        # Connection addresses
        rest_address = "%s/" %(self.host,)
        ws_address = "%s/ws/tracker/%s/" % (self.host,self.experiment_id)

        # Create the experiment
        self.rest = RestConnection(rest_address, self.key, self.experiment_name)
        if not self.tracking:
            return

        # Instantiate the experiment
        experiment_created_flag = self.rest.create_experiment(self.experiment_id,
                                                                self._experiment_file)
        if not experiment_created_flag:
            return

        # Start the websocket
        self.web_socket = WSConnectionThread(ws_address)
        self.web_socket.start()

        # Start the tracker thread
        self.tracker_thread = TrackerThread(self.web_socket, self.rest,  self.key, self._experiment_file)
        self.tracker_thread.start()

        # Send experiment start
        event_queue.put({
            'type' : ClientEvent.EXPERIMENT_START,
            'value' : self._experiment_start
        })

        # Send the code file
        event_queue.put({
            'type' : ClientEvent.CODE_FILE,
            'value' : {
                'filename' : self._experiment_file,
                'experiment_id' : self.experiment_id
            }
        })

        # Add the reference to the current tracker to the settings
        settings.current_tracker = self

        # Scrape the parameters from the script objects
        if self.auto_log:
            sklearn_loader()

        atexit.register(self._on_end)

    def _on_end(self):
        "Send the experiment end event on completion"
        self._experiment_end = current_string_datetime()
        event_queue.put({
            'type' : ClientEvent.EXPERIMENT_END,
            'value' : self._experiment_end
        })
        self.tracker_thread.stop()

    def add_param(self, param_name, param_value):
        '''
        Log the parameter name and its value

        Parameters
        ----------
        param_name : Name of the parameter
        param_value : Value of the parameter

        Returns
        -------
        None
        '''
        # Perform the necessary checks
        if not isinstance(param_name, str):
            logger.warning('param_name should be a string')
            return

        if param_name == "":
            logger.warning('param_name cannot be empty')
            return

        if not self.tracking:
            return

        # Add the event to the queue
        eval_event = {'type': ClientEvent.MODEL_PARAM, 'value': {}}
        eval_event['value'] = { param_name : param_value }
        event_queue.put(eval_event)

    def add_multiple_params(self, params_dict):
        '''
        Log multiple parameters

        Parameters
        ----------
        params_dict : Dict containing parameter's name as key and parameter's
                        value as value

        Returns
        -------
        None
        '''
        # Perform the necessary checks
        if not isinstance(params_dict, dict):
            logger.warning('Please provide a dict for multiple parameters')
            return

        if not self.tracking:
            return

        for k in params_dict.keys():
            self.add_param(k, params_dict[k])


    def add_metric(self, metric_name, metric_value, epoch=None):
        '''
        Log the metric's name and its value

        Parameters
        ----------
        metric_name : Name of the metric
        metric_value : Value of the metric

        Returns
        -------
        None
        '''
        # Perform the necessary checks
        if not isinstance(metric_name, str):
            logger.warning('metric_name should be a string')
            return

        if metric_name == "":
            logger.warning('metric_name cannot be empty')
            return


        if not ( isinstance(metric_value, int) or isinstance(metric_value, float) ):
            logger.warning('metric_value should be a number')
            return

        if epoch is not None and not ( isinstance(epoch, int) or
                                        isinstance(epoch, float) ):
            logger.warning('epoch should be a number')
            return

        if not self.tracking:
            return

        # Add the event to the queue
        metric_event = {'type': ClientEvent.EVAL_PARAM,
                        'value': {},
                        'epoch': epoch}
        metric_event['value'] = { metric_name : metric_value }
        event_queue.put(metric_event)

    def add_multiple_metrics(self, metrics_dict, epoch=None):
        '''
        Log multiple metrics

        Parameters
        ----------
        metrics_dict : Dict containing metric's name as key and parameter's
                        value as value

        Returns
        -------
        None
        '''
        # Perform the necessary checks
        if not isinstance(metrics_dict, dict):
            logger.warning('Please provide a dict for multiple parameters')
            return

        if not self.tracking:
            return

        for k in metrics_dict:
            self.add_metric(k, metrics_dict[k], epoch)


    def add_duration_at_epoch(self, tag, seconds_elapsed, epoch):
        '''
        Log the duration at a particular epoch

        Parameters
        ----------
        tag : Name of the duration
        seconds_elapsed: Number of seconds elapsed for the duration
        epoch: Current epoch number

        Returns
        -------
        None
        '''
        # Perform the necessary checks
        if not isinstance(tag, str):
            logger.warning('tag should be a string')
            return

        if tag == "":
            logger.warning('tag cannot be empty')
            return

        if not ( isinstance(seconds_elapsed, int) or
                isinstance(seconds_elapsed, float) ):
            logger.warning('seconds_elapsed should be a number')
            return

        if epoch is None:
            logger.warning('epoch should be present')
            return

        if epoch is not None and not ( isinstance(epoch, int) or
                                        isinstance(epoch, float) ):
            logger.warning('epoch should be a number')
            return

        if not self.tracking:
            return

        # Add the event to the queue
        duration_event = {'type': ClientEvent.DURATION_PARAM,
                        'value': {},
                        'epoch': epoch}
        duration_event['value'] = { tag : seconds_elapsed }
        event_queue.put(duration_event)

    def add_dataset_id(self, id):
        '''
        Log a user provided id for the dataset used

        Parameters
        ----------
        id : Id of the dataset

        Returns
        -------
        None
        '''
        if not isinstance(id, (int,float,str)):
            logger.warning('Dataset id should be a number or string')
            return

        if not self.tracking:
            return

        dataset_id_event = {'type': ClientEvent.DATASET_ID,
                            'value': id}
        event_queue.put(dataset_id_event)

    def add_custom_object(self, name, object):
        '''
        Save the pickled version of the custom object

        Parameters
        ----------
        name : Name for the object
        custom object: Python object to be stored

        Returns
        -------
        None
        '''
        if not isinstance(name, str):
            logger.warning('Custom object name should be a string')
            return

        if not self.rest:
            logger.info("Please instantiate the ModelChimp Tracker to store custom objects")
            return

        if not self.tracking:
            return

        compressed_object, filesize = self.__get_compressed_picke(object)
        result = {
            "name": name,
            "filesize": filesize,
            "project": self.rest.project_id,
            "ml_model": self.rest.model_id,
        }
        custom_object_url = 'api/experiment-custom-object/create/%s/' % (self.rest.project_id,)


        logger.info("Uploading custom object: %s" % name)
        save_request = self.rest.post(custom_object_url, data=result,
                        files={'custom_object_file': compressed_object})

        if save_request.status_code == 201:
            logger.info("%s: custom object was successfully saved" % name)
        else:
            logger.info("Custom object could not be saved.")

    def pull_custom_object(self, id):
        '''
        Pull the custom object from ModelChimp server to the script

        Parameters
        ----------
        id : Id of the dataset

        Returns
        -------
        Object
        '''
        pull_object_url = 'api/experiment-custom-object/retrieve/%s/?custom-object-id=%s' % (self.rest.project_id, id)

        if not isinstance(id, str):
            logger.warning('Custom object id should be a string')
            return

        # Check the id is of correct pattern
        if not is_uuid4_pattern(id):
            logger.warning('Given custom object id is of wrong pattern. Please, insert the correct id')
            return

        if not self.tracking:
            return

        logger.info("Downloading custom object with the id: %s" % id)
        request = self.rest.get(pull_object_url)

        if request.status_code == 400:
            logger.info("Unable to retrieve custom object. Is it a valid custom object id?")

        custom_object = request.content
        custom_object = zlib.decompress(custom_object, 31)
        custom_object = pickle.loads(custom_object)

        return custom_object


    def add_custom_plot(self, name="exportedPlot.png", plt=None):
        '''
        Store a matplot

        Parameters
        ----------
        name : Name of the plot
        plt: Matplot object

        Returns
        -------
        None
        '''
        if not isinstance(name, str):
            logger.warning('Custom plot name should be a string')
            return

        if not self.rest:
            logger.info("Please instantiate the ModelChimp Tracker to store custom plots")
            return

        if not self.tracking:
            return

        axes = plt.gca()
        if axes.has_data() is False:
            logger.warning("Empty plot")
            return

        #Export the matplot as an image
        filepath = ("./" + name + ".svg").strip()
        plt.savefig(filepath, bbox_inches="tight", format="svg")
        imageFile = open(filepath, 'rb')
        filesize = os.path.getsize(filepath)

        result = {
            "name": name,
            "filesize": filesize,
            "project": self.rest.project_id,
            "ml_model": self.rest.model_id,
        }
        mat_plot_url = 'api/experiment-mat-plot/create/%s/' % (self.rest.project_id,)

        logger.info("Uploading custom plot: %s" % name)
        save_request = self.rest.post(mat_plot_url, data=result,
                        files={'mat_plot_file': imageFile})
        imageFile.close()

        if save_request.status_code == 201:
            logger.info("%s: custom plot was successfully saved" % name)
        else:
            logger.info("Custom plot could not be saved.")

        if os.path.exists(filepath):
            logger.debug("Removing temporary file.")
            os.remove(filepath)

    def pull_params(self, experiment_id):
        '''
        Pull the parameters of another experiment to the client script

        Parameters
        ----------
        experiment_id : Id of an experiment

        Returns
        -------
        Dict
        '''
        pull_params_url = 'api/experiment-pull-param/?experiment-id=' + experiment_id

        if not isinstance(experiment_id, str):
            logger.warning('experiment_id should be a string')
            return

        if not self.tracking:
            return

        request = self.rest.get(pull_params_url)

        if request.status_code == 400:
            logger.info("Have you provided the correct experiment id?")
            return

        if request.status_code == 403:
            logger.info("You don't have permission for this experiment")
            return

        params = json.loads(request.text)

        return params


    def add_image(self, filepath, metric_dict=None, custom_file_name=None, epoch=None):
        '''
        Upload image. This is useful for computer vision use cases

        Parameters
        ----------
        filepath : File path of the image
        metric_dict : Dict of metrics to be stored for the image
        custom_file_name : An alternate name to be used for storing the image
        Epoch: Epoch at which the image was used for prediction

        Returns
        -------
        None
        '''
        url = 'api/experiment-images/add-image/'

        if not os.path.isfile(filepath):
            logger.warning('Image file does not exist at %s' % (filepath))
            return

        if metric_dict and not isinstance(metric_dict, dict):
            logger.warning('metric_dict should be a dictionary for file: %s' % (filepath))
            return

        if metric_dict:
            for k in metric_dict:
                if not isinstance(metric_dict[k], (int,float)):
                    logger.warning("Metric - %s is not a number" % (k,))
                    del(metric_dict[k])

        if custom_file_name and not isinstance(custom_file_name, str):
            logger.warning('custom_file_name should be a string for file: %s' % (filepath))
            return

        if epoch and not isinstance(epoch, int):
            logger.warning('epoch should be a dictionary for file: %s' % (filepath))
            return

        if not self.rest:
            logger.info("Please instantiate the ModelChimp Tracker to store images")
            return

        if not self.tracking:
            return

        result = {
            "custom_file_name": custom_file_name,
            "metric_dict": json.dumps(metric_dict),
            "epoch": epoch,
            "project": self.rest.project_id,
            "ml_model": self.rest.model_id,
        }

        logger.info("Uploading image: %s" % filepath)
        with open(filepath, 'rb') as f:
            save_request = self.rest.post( "%s%s/" %(url, self.rest.project_id),
                            data=result,
                            files={'experiment_image': f})

        if save_request.status_code != 201:
            logger.info("Image could not be saved: %s" % (filepath))


    def add_model_params(self, model, model_name=None):
        '''
        Extract the parameters out of a model object.
        Currently, only for scikit objects.

        Parameters
        ----------
        model : Model object whose parameters needs to be
            extracted
        model_name(optional) : Name of model for which the
            model parameters wil be stored. The name will
            be prefixed to each of the model parameter.

        Returns
        -------
        None
        '''
        # Check the parameters
        if model_name and not isinstance(model_name, str):
            logger.warning('add_model_params: model_name should be a string')
            return

        try:
            from sklearn.base import BaseEstimator
            if not isinstance(model, BaseEstimator):
                logger.warning('add_model_params: model is not supported')
                return
        except ImportError:
            logger.warning('add_model_params: sklearn models only')
            return

        if not self.tracking:
            return

        # Extrack the parameter of the models
        params = model.get_params()

        # Prefix the parameter with the model_name if present
        result = {}
        if model_name:
            for k in params.keys():
                new_key_name = "%s__%s" % (model_name, k)
                result[new_key_name] = params[k]
        else:
            result = params

        # Add to the queue
        self.add_multiple_params(result)


    def __get_compressed_picke(self, obj):
        pickled_obj = cloudpickle.dumps(obj,-1)
        z = zlib.compressobj(-1,zlib.DEFLATED,31)
        filesize =  sys.getsizeof(pickled_obj)
        gzip_compressed_pickle = z.compress(pickled_obj) + z.flush()

        return (gzip_compressed_pickle, filesize)
