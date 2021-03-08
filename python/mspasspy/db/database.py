"""
Tools for connecting to MongoDB.
"""
import os
import copy
import pathlib
import pickle
import struct
import sys
from array import array

import dask.bag as daskbag
import gridfs
import pymongo
from bson.objectid import ObjectId
import numpy as np
from obspy import Inventory
from obspy import UTCDateTime

from mspasspy.ccore.seismic import (TimeSeries,
                                    Seismogram,
                                    _CoreSeismogram,
                                    TimeReferenceType,
                                    DoubleVector,
                                    TimeSeriesEnsemble,
                                    SeismogramEnsemble)
from mspasspy.ccore.utility import (Metadata,
                                    MsPASSError,
                                    ErrorSeverity,
                                    dmatrix,
                                    ProcessingHistory)
from mspasspy.db.schema import DatabaseSchema, MetadataSchema


def read_distributed_data(client_arg, db_name, cursors, mode='promiscuous', normalize=[], load_history=True, exclude_keys=[], collection='wf',
                          format='spark', spark_context=None):
    """
     This method takes a list of mongodb cursors as input, constructs a mspasspy object for each cursor in a distributed
     manner, and return all of the mspasspy objects using the format required by the distributed computing framework
     (spark RDD or dask bag).

    :param client_arg: the argument to initialize a :class:`mspasspy.db.Client`.
    :param db_name: the database name in mongodb.
    :param cursors: mongodb cursors where each corresponds to a stored mspasspy object.
    :param load_history: `True` to load object-level history into the mspasspy object.
    :param include_undefined: `True` to also read the attributes in the collection that are not defined in the schema.
    :param exclude_keys: the metadata attributes you want to exclude from being read.
    :type exclude_keys: a :class:`list` of :class:`str`
    :param collection: the collection name in the database that the object is stored. If not specified, use the default wf collection in the schema.
    :param format: "spark" or "dask".
    :type format: :class:`str`
    :param spark_context: user specified spark context.
    :type spark_context: :class:`pyspark.SparkContext`
    :return: a spark `RDD` or dask `bag` format of mspasspy objects.
    """
    if format == 'spark':
        list_ = spark_context.parallelize(cursors)
        return list_.map(lambda cur: _read_distributed_data(client_arg, db_name, cur, mode, normalize, load_history, exclude_keys, collection))
    elif format == 'dask':
        list_ = daskbag.from_sequence(cursors)
        return list_.map(lambda cur: _read_distributed_data(client_arg, db_name, cur, mode, normalize, load_history, exclude_keys, collection))
    else:
        raise TypeError("Only spark and dask are supported")


def _read_distributed_data(client_arg, db_name, id, mode='promiscuous', normalize=[], load_history=True, include_undefined=False, exclude_keys=[], collection='wf'):
    """
     A helper method used in the distributed map operation. It creates a mongodb connection with provided
     configurations, reads data from the database, constructs a mspasspy object and returns it.

    :param client_arg: the argument to initialize a :class:`mspasspy.db.Client`.
    :param db_name: the database name in mongodb.
    :param id: the `bson.ObjectId` of the mspasspy object stored in mongodb or a dict that contains such an "_id".
    :type id: :class:'bson.objectid.ObjectId'/dict.
    :param load_history: `True` to load object-level history into the mspasspy object.
    :param include_undefined: `True` to also read the attributes in the collection that are not defined in the schema.
    :param exclude_keys: the metadata attributes you want to exclude from being read.
    :type exclude_keys: a :class:`list` of :class:`str`
    :param collection: the collection name in the database that the object is stored. If not specified, use the default wf collection in the schema.
    :return: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
    """
    from mspasspy.db.client import Client
    client = Client(client_arg)
    db = Database(client, db_name)
    return db.read_data(id, mode, normalize, load_history, exclude_keys, collection)


class Database(pymongo.database.Database):
    """
    A MongoDB database handler.

    This is a wrapper around the :class:`~pymongo.database.Database` with
    methods added to handle MsPASS data.  The one and only constructor
    uses a database handle normally created with a variant of this pair
    of commands:
        client=MongoClient()
        db=client['database_name']
    where database_name is variable and the name of the database you
    wish to access with this handle.
    """

    def __init__(self, *args, db_schema=None, md_schema=None, **kwargs):
        super(Database, self).__init__(*args, **kwargs)
        if isinstance(db_schema, DatabaseSchema):
            self.database_schema = db_schema
        elif isinstance(db_schema, str):
            self.database_schema = DatabaseSchema(db_schema)
        else:
            self.database_schema = DatabaseSchema()

        if isinstance(md_schema, MetadataSchema):
            self.metadata_schema = md_schema
        elif isinstance(md_schema, str):
            self.metadata_schema = MetadataSchema(md_schema)
        else:
            self.metadata_schema = MetadataSchema()

    def __getstate__(self):
        ret = self.__dict__.copy()
        ret['_Database__client'] = self.client.__repr__()
        return ret

    def __setstate__(self, data):
        from pymongo import MongoClient
        data['_Database__client'] = eval(data['_Database__client'])
        self.__dict__.update(data)

    def set_metadata_schema(self, schema):
        """
        Set metadata_schema defined in the Database class.

        :param schema: a instance of :class:`mspsspy.db.schema.MetadataSchema`
        """
        self.metadata_schema = schema

    def set_database_schema(self, schema):
        """
        Set database_schema defined in the Database class.

        :param schema: a instance of :class:`mspsspy.db.schema.DatabaseSchema`
        """
        self.database_schema = schema

    def read_data(self, object_id, mode='promiscuous', normalize=[], load_history=False, exclude_keys=[], collection='wf'):
        """
        Reads and returns the mspasspy object stored in the database.

        :param object_id: "_id" of the mspasspy object or a dict that contains the "_id".
        :type object_id: :class:`bson.objectid.ObjectId`/dict
        :param mode: reading mode regarding schema checks, should be one of ['promiscuous','cautious','pedantic']
        :type mode: class:`str`
        :param normalize: normalized collection you want to read into a mspass object
        :type normalize: a :class:`list` of :class:`str`
        :param load_history: `True` to load object-level history into the mspasspy object.
        :param exclude_keys: the metadata attributes you want to exclude from being read.
        :type exclude_keys: a :class:`list` of :class:`str`
        :param collection: the collection name in the database that the object is stored. If not specified, use the default wf collection in the schema.
        :return: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        """
        try:
            wf_collection = self.database_schema.default_name(collection)
        except MsPASSError as err:
            raise MsPASSError('collection {} is not defined in database schema'.format(collection), 'Invalid') from err
        object_type = self.database_schema[wf_collection].data_type()

        if object_type not in [TimeSeries, Seismogram]:
            raise MsPASSError('only TimeSeries and Seismogram are supported, but {} is requested. Please check the data_type of {} collection.'.format(
                object_type, wf_collection), 'Fatal')
        
        if mode not in ["promiscuous", "cautious", "pedantic"]:
            raise MsPASSError('only promiscuous, cautious and pedantic are supported, but {} is requested.'.format(mode), 'Fatal')

        # This assumes the name of a metadata schema matches the data type it defines. 
        read_metadata_schema = self.metadata_schema[object_type.__name__]

        # We temporarily swap the main collection defined by the metadata schema by 
        # the wf_collection. This ensures the method works consistently for any
        # user-specified collection argument.
        metadata_schema_collection = read_metadata_schema.collection('_id')
        if metadata_schema_collection != wf_collection:
            temp_metadata_schema = copy.deepcopy(self.metadata_schema)
            temp_metadata_schema[object_type.__name__].swap_collection(
                metadata_schema_collection, wf_collection, self.database_schema)
            read_metadata_schema = temp_metadata_schema[object_type.__name__]

        # find the corresponding document according to object id
        col = self[wf_collection]
        try:
            oid = object_id['_id']
        except:
            oid = object_id
        object_doc = col.find_one({'_id': oid})
        if not object_doc:
            return None

        # 1. build metadata as dict
        md = Metadata()

        # 1.1 read in the attributes from the document in the database
        for k in object_doc:
            if k in exclude_keys:
                continue
            if mode == "promiscuous":
                md[k] = object_doc[k]
                continue
            # FIXME: note that we do not check whether the attributes' type in the database matches the schema's definition.
            # This may or may not be correct. Should test in practice and get user feedbacks.
            if read_metadata_schema.is_defined(k) and not read_metadata_schema.is_alias(k):
                md[k] = object_doc[k]

        # 1.2 read the attributes in the metadata schema
        col_dict = {}
        for k in read_metadata_schema.keys():
            col = read_metadata_schema.collection(k)
            # 1.2.1. col is not None and is a normalized collection name
            # 1.2.2. normalized key id exists in the wf document
            # 1.2.3. k is not one of the exclude keys
            # 1.2.4. col is in the normalize list provided by user
            if col and col != wf_collection and col+'_id' in object_doc and k not in exclude_keys and col in normalize:
                if col not in col_dict:
                    col_dict[col] = self[col].find_one({'_id': object_doc[col + '_id']})
                # might unable to find the normalized document by the normalized_id in the object_doc
                # TODO: this is not covered by test
                if not col_dict[col]:
                    continue
                md[k] = col_dict[col][self.database_schema[col].unique_name(k)]

        # 1.3 schema check normalized data according to the read mode
        is_dead = False
        log_error_msg = []
        fatal_keys = []
        if mode == "cautious":
            for k in md:
                if read_metadata_schema.is_defined(k):
                    col = read_metadata_schema.collection(k)
                    unique_key = self.database_schema[col].unique_name(k)
                    if not isinstance(md[k], read_metadata_schema.type(k)):
                        # try to convert the mismatch attribute
                        try:
                            insert_dict[k] = read_metadata_schema.type(k)(md[k])
                        except:
                            if self.database_schema[col].is_required(unique_key):
                                fatal_keys.append(k)
                                is_dead = True
                                log_error_msg.append("cautious mode: Required attribute {} has type {}, forbidden by definition and unable to convert".format(k, type(md[k])))

        elif mode == "pedantic":
            for k in md:
                if read_metadata_schema.is_defined(k):
                    if not isinstance(md[k], read_metadata_schema.type(k)):
                        fatal_keys.append(k)
                        is_dead = True
                        log_error_msg.append("pedantic mode: {} has type {}, forbidden by definition".format(k, type(md[k])))


        # 1.4 create a mspass object by passing MetaData
        # if not changing the fatal key values, runtime error in construct a mspass object
        for k in fatal_keys:
            if read_metadata_schema.type(k) is str:
                md[k] = ""
            elif read_metadata_schema.type(k) is int:
                md[k] = 0
            elif read_metadata_schema.type(k) is float:
                md[k] = 0.0
            elif read_metadata_schema.type(k) is bool:
                md[k] = False
            elif read_metadata_schema.type(k) is dict:
                md[k] = {}
            elif read_metadata_schema.type(k) is list:
                md[k] = []
            elif read_metadata_schema.type(k) is bytes:
                md[k] = b'\x00'
            else:
                md[k] = None

        if object_type is TimeSeries:
            # FIXME: This is awkward. Need to revisit when we have proper constructors.
            mspass_object = TimeSeries({k: md[k] for k in md}, np.ndarray([0], dtype=np.float64))
            # FIXME: if npts is in the exclude list or not in the schema, the following won't work.
            # May need to consider adding a "required" key to the metadata schema to avoid invalid combination.
            if 'npts' in object_doc:
                mspass_object.npts = object_doc['npts']
        else:
            mspass_object = Seismogram(_CoreSeismogram(md, False))

        # not continue step 2 & 3 if the mspass object is dead
        if is_dead:
            mspass_object.kill()
            for msg in log_error_msg:
                mspass_object.elog.log_error('read_data', msg, ErrorSeverity.Invalid)
        else: 
            # 2.load data from different modes
            storage_mode = object_doc['storage_mode']
            if storage_mode == "file":
                self._read_data_from_dfile(mspass_object, object_doc['dir'], object_doc['dfile'], object_doc['foff'])
            elif storage_mode == "gridfs":
                self._read_data_from_gridfs(mspass_object, object_doc['gridfs_id'])
            elif storage_mode == "url":
                pass  # todo for future
            else:
                raise TypeError("Unknown storage mode: {}".format(storage_mode))

            # 3.load history
            if load_history:
                history_obj_id_name = self.database_schema.default_name('history_object') + '_id'
                if history_obj_id_name in object_doc:
                    self._load_history(mspass_object, object_doc[history_obj_id_name])
            
            mspass_object.live = True
            mspass_object.clear_modified()
        
        return mspass_object

    def save_data(self, mspass_object, mode="promiscuous", storage_mode='gridfs', dfile=None, dir=None, exclude_keys=[], collection=None):
        """
        Save the mspasspy object (metadata attributes, processing history, elogs and data) in the mongodb database.

        :param mspass_object: the object you want to save.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param mode: reading mode regarding schema checks, should be one of ['promiscuous','cautious','pedantic']
        :type mode: class:`str`
        :param storage_mode: "gridfs" stores the object in the mongodb grid file system (recommended). "file" stores
            the object in a binary file, which requires `dfile` and `dir`.
        :type storage_mode: :class:`str`
        :param dfile: file name if using "file" storage mode.
        :type dfile: :class:`str`
        :param dir: file directory if using "file" storage mode.
        :type dir: :class:`str`
        :param exclude_keys: the metadata attributes you want to exclude from being stored.
        :type exclude_keys: a :class:`list` of :class:`str`
        :param collection: the collection name you want to use. If not specified, use the defined collection in the metadata schema.
        """
        if not isinstance(mspass_object, (TimeSeries, Seismogram)):
            raise TypeError("only TimeSeries and Seismogram are supported")
        if storage_mode not in ['file', 'gridfs']:
            raise TypeError("Unknown storage mode: {}".format(storage_mode))
        if mode not in ['promiscuous', 'cautious', 'pedantic']:
            raise MsPASSError('only promiscuous, cautious and pedantic are supported, but {} is requested.'.format(mode), 'Fatal')
        # below we try to capture permission issue before writing anything to the database.
        # However, in the case that a storage is almost full, exceptions can still be 
        # thrown, which could mess up the database record.
        if storage_mode == 'file':
            if not dfile and not dir:
                # Note the following uses the dir and dfile defined in the data object.
                # It will ignore these two keys already in the collection in an update
                # transaction, and the dir and dfile in the collection will be replaced.
                if ('dir' not in mspass_object) or ('dfile' not in mspass_object):
                    raise ValueError(
                        'dir or dfile is not specified in data object')
                dir = os.path.abspath(mspass_object['dir'])
                dfile = mspass_object['dfile']
            else:
                dir = os.path.abspath(dir)
            fname = os.path.join(dir, dfile)
            if os.path.exists(fname):
                if not os.access(fname, os.W_OK):
                    raise PermissionError(
                        'No write permission to the save file: {}'.format(fname))
            else:
                # the following loop finds the top level of existing parents to fname
                # and check for write permission to that directory. 
                for path_item in pathlib.PurePath(fname).parents:
                    if os.path.exists(path_item):
                        if not os.access(path_item, os.W_OK | os.X_OK):
                            raise PermissionError(
                                'No write permission to the save directory: {}'.format(dir))
                        break

        schema = self.metadata_schema
        if isinstance(mspass_object, TimeSeries):
            save_schema = schema.TimeSeries
        else:
            save_schema = schema.Seismogram

        update_res_code = -1
        if mspass_object.live:
            # 1. save metadata, with update mode
            update_res_code = self.update_metadata(mspass_object, mode, exclude_keys, collection, False)

            if mspass_object.live:
                # 2. save actual data in file/gridfs mode
                wf_collection = save_schema.collection('_id') if not collection else collection
                col = self[wf_collection]
                object_doc = col.find_one({'_id': mspass_object['_id']})
                filter_ = {'_id': mspass_object['_id']}
                update_dict = {'storage_mode': storage_mode}

                if storage_mode == "file":
                    foff = self._save_data_to_dfile(mspass_object, dir, dfile)
                    update_dict['dir'] = dir
                    update_dict['dfile'] = dfile
                    update_dict['foff'] = foff
                elif storage_mode == "gridfs":
                    old_gridfs_id = None if 'gridfs_id' not in object_doc else object_doc['gridfs_id']
                    gridfs_id = self._save_data_to_gridfs(mspass_object, old_gridfs_id)
                    update_dict['gridfs_id'] = gridfs_id
                #TODO will support url mode later 
                #elif storage_mode == "url":
                #    pass
                col.update_one(filter_, {'$set': update_dict})
        
        else:
            # FIXME: we could have recorded the full stack here, but need to revise the logger object
            # to make it more powerful for Python logging.
            mspass_object.elog.log_verbose(
                sys._getframe().f_code.co_name, "Skipped saving dead object")
            self._save_elog(mspass_object)
        
        return update_res_code

    # clean the collection fixing any type errors and removing any aliases using the schema currently defined for self
    def clean_collection(self, collection, log_id_keys=[], is_print=False, query={}, delete_undefined=False, rename={}, check_xref=[]):
        """
        clean a collection in user's database by a user defined query

        :param collection: the collection name you would like to clean.
        :type collection: :class:`str`
        :param log_id_keys: a list of attributes you want to identify in the documents during cleaning.
        :type log_id_keys: a :class:`list` of :class:`str`
        :param is_print: if specify as True, we will print all the message verbosely, default to be False
        :param query: the query dict that passed to MongoDB to find mtached documents.
        :type query: :class:`dict`
        """
        print_messages = []
        fixed_cnt = {}
        # fix the queried documents in the collection
        col = self[self.database_schema.default_name(collection)]
        matchsize = col.count_documents(query)
        # no match documents return
        if (matchsize == 0):
            return fixed_cnt
        else:
            docs = col.find(query)
            for doc in docs:
                if '_id' in doc:
                    fixed_attr_cnt, messages = self.clean(doc['_id'], collection, log_id_keys, delete_undefined, rename, check_xref)
                    print_messages.extend(messages)
                    for k, v in fixed_attr_cnt.items():
                        if k not in fixed_cnt:
                            fixed_cnt[k] = 1
                        else:
                            fixed_cnt[k] += v
        if is_print:
            for msg in print_messages:
                print(msg)
        
        return fixed_cnt

    # clean a single document in the given collection atomically
    def clean(self, document_id, collection='wf', log_id_keys=[], delete_undefined=False, rename={}, check_xref=[]):
        """
        Clean a document in a collection, including deleting the document if required keys are absent or fix the types if there are mismatches.

        :param document_id: the value of the _id field in the document you want to clean
        :type document_id: class:`bson.objectid.ObjectId`
        :param collection: the name of collection saving the document. If not specified, use the default wf collection
        :param log_id_keys: a list of keys you want to added to better identify problems when error happens. It's used in the print messages.
        :type log_id_keys: :class:`list` of :class:`str`
 
        :return fixed_cnt: number of keys that are fixed
        :rtype fixed_cnt: class:`dict`
        :return print_messages: the verbose informative/invalid messages
        :rtype print_messages: class:`list`
        """
        print_messages = []
        fixed_cnt = {}

        # if the document does not exist in the db collection, return
        collection = self.database_schema.default_name(collection)
        col = self[collection]
        doc = col.find_one({'_id': document_id})
        if not doc:
            print_messages.append("collection {} document _id: {}, is not found".format(collection, document_id))
            return fixed_cnt, print_messages
        
        # access each key
        log_id_dict = {}
        # get all the values of the log_id_keys
        for k in doc:
            if k in log_id_keys:
                log_id_dict[k] = doc[k]
        log_helper = "collection {} document _id: {}, ".format(collection, doc['_id'])
        for k, v in log_id_dict.items():
            log_helper += "{}: {}, ".format(k, v)

        # check if the document has all the required fields
        missing_required_attr_list = []
        for k in self.database_schema[collection].keys():
            if self.database_schema[collection].is_required(k) and k not in doc:
                missing_required_attr_list.append(k)
        if missing_required_attr_list:
            # delete this document
            col.delete_one({'_id': doc['_id']})
            error_msg = "required attribute: "
            for missing_attr in missing_required_attr_list:
                error_msg += "{} ".format(missing_attr)
            error_msg += "are missing."
            print_messages.append("{}{} the document is deleted.".format(log_helper, error_msg))
            return fixed_cnt, print_messages

        missing_xref_key_list = []
        for xref_k in check_xref:
            # xref_k in check_xref list should be defined in schema first
            if self.database_schema[collection].is_defined(xref_k):
                unique_xref_k = self.database_schema[collection].unique_name(xref_k)
                # xref_k should be a reference key as well
                if self.database_schema[collection].is_xref_key(unique_xref_k) and unique_xref_k not in doc:
                    missing_xref_key_list.append(xref_k)
        # missing required xref keys, should be deleted
        if missing_xref_key_list:
            col.delete_one({'_id': doc['_id']})
            error_msg = "required xref key: "
            for missing_key in missing_xref_key_list:
                error_msg += "{} ".format(missing_key)
            error_msg += "are missing."
            print_messages.append("{}{} the document is deleted.".format(log_helper, error_msg))
            return fixed_cnt, print_messages

        # try to fix the error in the doc
        update_dict = {}
        for k in doc:
            if k == '_id':
                continue
            # if not the schema keys, ignore schema type check enforcement
            if not self.database_schema[collection].is_defined(k):
                # delete undefined attributes in the doc if delete_undefined is True
                if not delete_undefined:
                    # try to rename the user specified keys
                    if k in rename:
                        update_dict[rename[k]] = doc[k]
                    else:
                        update_dict[k] = doc[k]
                continue
            # to remove aliases, get the unique key name defined in the schema
            unique_k = self.database_schema[collection].unique_name(k)
            if not isinstance(doc[k], self.database_schema[collection].type(unique_k)):
                try:
                    update_dict[unique_k] = self.database_schema[collection].type(unique_k)(doc[k])
                    print_messages.append("{}attribute {} conversion from {} to {} is done.".format(log_helper, unique_k, type(doc[k]), self.database_schema[collection].type(unique_k)))
                    if k in fixed_cnt:
                        fixed_cnt[k] += 1
                    else:
                        fixed_cnt[k] = 1
                except:
                    print_messages.append("{}attribute {} conversion from {} to {} cannot be done.".format(log_helper, unique_k, type(doc[k]), self.database_schema[collection].type(unique_k)))
            else:
                # attribute values remain the same
                update_dict[unique_k] = doc[k]
                
        # update the fixed attributes in the document in the collection
        filter_ = {'_id': doc['_id']}
        # use replace_one here because there may be some aliases in the document
        col.replace_one(filter_, update_dict)
        
        return fixed_cnt, print_messages

    def verify(self, document_id, collection='wf', tests=['xref', 'type', 'undefined']):
        """
        Verify a document in a collection, including checking links, checking required attributes and checking if attribute type matches schema.

        :param document_id: the value of the _id field in the document you want to verify
        :type document_id: class:`bson.objectid.ObjectId`
        :param collection: the name of collection saving the document. If not specified, use the default wf collection
        :param tests: the type of tests you want to verify, should be a subset of ['xref', 'type', 'undefined']
        :type tests: :class:`list` of :class:`str`
 
        :return fixed_cnt: number of keys that are fixed
        :rtype fixed_cnt: class:`dict`
        :return print_messages: the verbose informative/invalid messages
        :rtype print_messages: class:`list`
        """
        # check tests
        for test in tests:
            if test not in ['xref', 'type', 'undefined']:
                raise MsPASSError('only xref, type and undefined are supported, but {} is requested.'.format(test), 'Fatal')
        # remove redundant if happens
        tests = list(set(tests))

        problematic_keys = {}

        collection = self.database_schema.default_name(collection)
        col = self[collection]
        doc = col.find_one({'_id': document_id})

        # if the document does not exist in the db collection, return
        if not doc:
            return problematic_keys
        
        # run the tests
        for test in tests:
            if test == 'xref':
                # test every possible xref keys in the doc
                for key in doc:
                    if self.database_schema[collection].is_defined(key) and self.database_schema[collection].is_xref_key(key):
                        # only test normalized keys with '_id'
                        if '_id' in key and key.rsplit('_', 1)[1] == 'id':
                            normalized_collection_name = key.rsplit('_', 1)[0]
                            normalized_collection_name = self.database_schema.default_name(normalized_collection_name)
                            normalized_col = self[normalized_collection_name]
                            # try to find the referenced docuement
                            normalized_doc = normalized_col.find_one({'_id': doc[key]})
                            if not normalized_doc:
                                problematic_keys[key] = test

            elif test == 'undefined':
                # check if doc has every required key in the collection schema
                unique_doc_keys = []
                # change possible aliases to unique keys
                for key in doc:
                    if self.database_schema[collection].is_defined(key):
                        unique_doc_keys.append(self.database_schema[collection].unique_name(key))
                    else:
                        unique_doc_keys.append(key)
                # check every required keys in the collection schema
                for key in self.database_schema[collection].required_keys():
                    if key not in unique_doc_keys:
                        problematic_keys[key] = test
            
            elif test == 'type':
                # check if there are type mismatch between keys in doc and keys in schema
                for key in doc:
                    if self.database_schema[collection].is_defined(key) and not isinstance(doc[key], self.database_schema[collection].type(key)):
                        problematic_keys[key] = test

        return problematic_keys

    def delete_attributes(self, collection, keylist, query={}, verbose=False):
        """
        Deletes all occurrences of attributes linked to keys defined 
        in a list of keywords passed as (required) keylist argument.  
        If a key is not in a given document no action is taken. 
        
        :param db:  Database handle to be updated
        :param collection:  MongoDB collection to be updated
        :param keylist:  list of keys for elements of each document 
        that are to be deleted.   key are not test against schema 
        but all matches will be deleted.
        :param query: optional query string passed to find database 
        collection method.  Can be used to limit edits to documents 
        matching the query.  Default is the entire collection.
        :param verbose:  when true edit will produce a line of printed 
        output describing what was deleted.  Use this option only if 
        you know from dbverify the number of changes to be made are small.
        
        :return:  dict keyed by the keys of all deleted entries.  The value 
        of each entry is the number of documents the key was deleted from.
        """
        dbcol=self[collection]
        cursor=dbcol.find(query)
        counts=dict()
        # preload counts to 0 so we get a return saying 0 when no changes 
        # are made
        for k in keylist:
            counts[k]=0
        for doc in cursor:
            id=doc.pop('_id')
            n=0
            todel=dict()
            for k in keylist:
                if k in doc:
                    todel[k]=doc[k]
                    val=doc.pop(k)
                    if verbose:
                        print('Deleted ',val,' with key=',k,' from doc with id=',id)
                    counts[k]+=1
                    n+=1
            if n>0:
                dbcol.update_one({'_id':id},{'$unset' : todel})
        return counts
    
    def rename_attributes(self, collection, rename_map, query={}, verbose=False):
        """
        Renames specified keys for all or a subset of documents in a 
        MongoDB collection.   The updates are driven by an input python 
        dict passed as the rename_map argument. The keys of rename_map define
        doc keys that should be changed.  The values of the key-value 
        pairs in rename_map are the new keys assigned to each match.  
        
        
        :param db:  Database handle to be updated
        :param collection:  MongoDB collection to be updated
        :param rename_map:  remap definition dict used as described above.
        :param query: optional query string passed to find database 
        collection method.  Can be used to limit edits to documents 
        matching the query.  Default is the entire collection.
        :param verbose:  when true edit will produce a line of printed 
        output describing what was deleted.  Use this option only if 
        you know from dbverify the number of changes to be made are small.
        When false the function runs silently.
        
        :return:  dict keyed by the keys of all changed entries.  The value 
        of each entry is the number of documents changed.  The keys are the 
        original keys.  displays of result should old and new keys using 
        the rename_map.
        """
        dbcol=self[collection]
        cursor=dbcol.find(query)
        counts=dict()
        # preload counts to 0 so we get a return saying 0 when no changes 
        # are made
        for k in rename_map:
            counts[k]=0
        for doc in cursor:
            id=doc.pop('_id')
            n=0
            for k in rename_map:
                n=0
                if k in doc:
                    val=doc.pop(k)
                    newkey=rename_map[k]
                    if verbose:
                        print('Document id=',id)
                        print('Changed attribute with key=',k,' to have new key=',newkey)
                        print('Attribute value=',val)
                    doc[newkey]=val
                    counts[k]+=1
                    n+=1
            dbcol.replace_one({'_id':id},doc)
        return counts

    def fix_attribute_types(self, collection, query={}, verbose=False):
        """
        This function attempts to fix type collisions in the schema defined 
        for the specified database and collection.  It tries to fix any 
        type mismatch that can be repaired by the python equivalent of a 
        type cast (an obscure syntax that can be seen in the actual code).  
        Known examples are it can cleanly convert something like an int to 
        a float or vice-versa, but it cannot do something like convert an 
        alpha string to a number. Note, however, that python does cleanly 
        convert simple number strings to number.  For example:  x=int('10')
        will yield an "int" class number of 10.  x=int('foo'), however, will
        not work.   Impossible conversions will not abort the function but 
        will generate an error message printed to stdout.  The function 
        continues on so if there are a large number of such errors the 
        output could become voluminous.  ALWAYS run dbverify before trying 
        this function (directly or indirectly through the command line 
        tool dbclean).   
        
        :param db:  Database handle to be updated
        :param collection:  MongoDB collection to be updated
        :param query: optional query string passed to find database 
        collection method.  Can be used to limit edits to documents 
        matching the query.  Default is the entire collection.
        :param verbose:  when true edit will produce one or more lines of 
        printed output for each change it makes.  The default is false.
        Needless verbose should be avoided unless you are certain the 
        number of changes it will make are small.  
        """
        dbcol=self[collection]
        schema=self.database_schema
        col_schema=schema[collection]
        counts=dict()
        cursor=dbcol.find(query)
        for doc in cursor:
            n=0
            id=doc.pop('_id')
            if verbose:
                print("////////Document id=",id,'/////////')
            up_d=dict()
            for k in doc:
                val=doc[k]
                if not col_schema.is_defined(k):
                    if verbose:
                        print('Warning:  in doc with id=',id,
                            'found key=',k,' that is not defined in the schema')
                        print('Value of key-value pair=',val)
                        print('Cannot check type for an unknown attribute name')
                    continue
                if not isinstance(val,col_schema.type(k)):
                    try:
                        newval=col_schema.type(k)(val)
                        up_d[k]=newval
                        if verbose:
                            print('Changed data for key=',k,' from ',val,' to ',newval)
                        if k in counts:
                            counts[k]+=1
                        else:
                            counts[k]=1
                        n+=1
                    except Exception as err:
                        print("////////Document id=",id,'/////////')
                        print('WARNING:  could not convert attribute with key=',
                            k,' and value=',val,' to required type=',
                            col_schema.type(k))
                        print('This error was thrown and handled:  ')
                        print(err)
            
            if n>0:
                dbcol.update_one({'_id' : id},{'$set' : up_d})
                
        return counts

    def check_links(self, normalize='site', wf="wf_TimeSeries", wfquery={}, verbose=False, error_limit=1000):
        """
        This function checks for missing cross-referencing ids in a 
        specified wf collection (i.e. wf_TimeSeries or wf_Seismogram)
        It scans the wf collection to detect two potential errors:
        (1) documents with the normalization key completely missing 
        and (2) documents where the key is present does not match any 
        document in normalization collection.   By default this
        function operates silently assuming the caller will 
        create a readable report from the return that defines 
        the documents that had errors.  This function is used in the 
        verify standalone program that acts as a front end to tests
        in this module.  The function can be run in independently 
        so there is a verbose option to print errors as they are encountered.
        
        :param db:  required MongoDB database handle.
        :param wf:  mspass waveform collection on which the normalization
        check is to be performed.  default is wf_TimeSeries.  
        Currently only accepted alternative is wf_Seismogram.
        :param wfquery:  optional dict passed as a query to limit the 
        documents scanned by the function.   Default will process the 
        entire wf collection.
        :param verbose:  when True errors will be printed.  By default 
        the function works silently and you should use the output to 
        interact with any errors returned.  
        :param error_limit: Is a sanity check on the number of errors logged.
        Errors of any type are limited to this number (default 1000).
        The idea is errors should be rare and if this number is exceeded 
        you have a big problem you need to fix before scanning again.  
        The number should be large enough to catch all condition but 
        not so huge it become cumbersome.  With no limit or a memory 
        fault is even possible on a huge dataset.
        :return:  returns a tuple with two lists.  Both lists are ObjectIds
        of the scanned wf collection that have errors.  component 0 
        of the tuple contains ids of wf entries that have the normalization 
        id set but the id does not resolve with the normalization collection.
        component 1 contains the ids of documents in the wf collection that
        do not contain the normalization id key at all (a more common problem)

        """
        # schema doesn't currently have a way to list normalized 
        # collection names.  For now we just freeze the names 
        # and put them in this one place for maintainability
        norm_collection_list=['site','channel','source']
        wf_collection_list=['wf_TimeSeries','wf_Seismogram']

        if not (normalize in norm_collection_list):
            raise MsPASSError('check_links:  illegal value for normalize arg='+normalize,
                            'Fatal')
        if not (wf in wf_collection_list):
            raise MsPASSError('check_links:  illegal value for wf arg='+wf,
                            'Fatal')
        # this uses our convention - we need a standard method for to 
        # define this key
        idkey=normalize+'_id'
        dbnorm=self[normalize]
        dbwf=self[wf]
        n=dbwf.count_documents(wfquery)
        if n==0:
            raise MsPASSError('checklinks:  '+wf
                +' collection has no data matching query=',str(wfquery),
                'Fatal')
        if verbose:
            print('Starting cross reference link check for ',wf,
                ' collection using id=',idkey)
            print('This should resolve links to ',normalize,' collection')
        # We accumulate bad ids in this list that is returned
        bad_id_list=list()
        missing_id_list=list()
        cursor=dbwf.find(wfquery)
        for doc in cursor:
            wfid=doc['_id']
            if idkey in doc:
                nrmid=doc[idkey]
                n_nrm=dbnorm.count_documents({'_id' : nrmid})
                if n_nrm==0:
                    bad_id_list.append(wfid)
                    if verbose:
                        print(str(wfid),' link with ',str(nrmid),' failed')
                    if len(bad_id_list) > error_limit:
                        raise MsPASSError('checklinks:  number of bad id errors exceeds internal limit',
                                        'Fatal')
            else:
                missing_id_list.append(wfid)
                if verbose:
                    print(str(wfid),' is missing required key=',idkey)
                if len(missing_id_list) > error_limit:
                        raise MsPASSError('checklinks:  number of missing id errors exceeds internal limit',
                                        'Fatal')
            if len(bad_id_list)>=error_limit or len(missing_id_list)>=error_limit:
                break
        return tuple([bad_id_list,missing_id_list])

    def check_attribute_types(self, collection="wf_TimeSeries", query={}, verbose=False, error_limit=1000):
        """
        This function checks the integrity of all attributes 
        found in a specfied collection.  It is designed to detect two 
        kinds of problems:  (1) type mismatches between what is stored 
        in the database and what is defined for the schema, and (2) 
        data with a key that is not recognized.  Both tests are necessary 
        because unlike a relational database MongoDB is very promiscuous 
        about type and exactly what goes into a document.  MongoDB pretty 
        much allow type it knows about to be associated with any key 
        you choose.   In MsPASS we need to enforce some type restrictions 
        to prevent C++ wrapped algorithms from aborting with type mismatches. 
        Hence, it is important to run this test on all collections needed 
        by a workflow before starting a large job.  
        
        :param db:  mspass Database handle.   Note this must be 
        a Database class defined by "from mspasspy.db.database import Database".
        The reason is that the mspass handle is an extension of MongoDB's 
        handle that includes the Schema class used to run the tests in 
        this function. This arg is required
        :param collection:  MongoDB collection that is to be scanned 
        for errors.  Note with normalized data this function should be 
        run on the appropriate wf collection and all normalization 
        collections the wf collection needs to link to. 
        :param query:  optional dict passed as a query to limit the 
        documents scanned by the function.   Default will process the 
        entire collection requested.
        :param verbose:  when True errors will be printed.   The default is
        False and the function will do it's work silently.   Verbose is 
        most useful in an interactive python session where the function 
        is called directly.  Most users will run this function 
        as part of tests driven by the dbverify program. 
        :param error_limit: Is a sanity check the number of errors logged
        The number of any type are limited to this number (default 1000).
        The idea is errors should be rare and if this number is exceeded 
        you have a big problem you need to fix before scanning again.  
        The number should be large enough to catch all condition but 
        not so huge it become cumbersome.  With no limit or a memory 
        fault is even possible on a huge dataset.
        :return:  returns a tuple with two python dict containers.  
        The component 0 python dict contains details of type mismatch errors.
        Component 1 contains details for data with undefined keys.  
        Both python dict containers are keyed by the ObjectId of the 
        document from which they were retrieved.  The values associated
        with each entry are like MongoDB subdocuments.  That is, the value
        return is itself a dict. The dict value contains key-value pairs
        that defined the error (type mismatch for 0 and undefined for 1)

        """
        # The following two can throw MsPASS errors but we let them 
        # do so. Callers should have a handler for MsPASSError
        dbschema=self.database_schema
        # This holds the schema for the collection to be scanned
        # dbschema is mostly an index to one of these
        col_schema=dbschema[collection]
        dbcol=self[collection]
        n=dbcol.count_documents(query)
        if n == 0:
            raise MsPASSError('check_attribute_types:  query='
                            +str(query)+' yields zero matching documents',
                            'Fatal')
        cursor=dbcol.find(query)
        bad_type_docs=dict()
        undefined_key_docs=dict()
        for doc in cursor:
            bad_types=dict()
            undefined_keys=dict()
            id=doc['_id']
            for k in doc:
                if col_schema.is_defined(k):
                    val=doc[k]
                    if type(val)!=col_schema.type(k):
                        bad_types[k]=doc[k]
                        if(verbose):
                            print('doc with id=',id,' type mismatch for key=',k)
                            print('value=',doc[k],' does not match expected type=',
                                col_schema.type(k))
                else:
                    undefined_keys[k]=doc[k]
                    if(verbose):
                        print('doc with id=',id,' has undefined key=',k,
                            ' with value=',doc[k])
            if len(bad_types)>0:
                bad_type_docs[id]=bad_types
            if len(undefined_keys)>0:
                undefined_key_docs[id]=undefined_keys;
            if len(undefined_key_docs)>=error_limit or len(bad_type_docs)>=error_limit:
                break

        return tuple([bad_type_docs,undefined_key_docs])
                
        
    def check_required(self, collection='site', keys=['lat','lon','elev','starttime','endtime'], query={}, verbose=False, error_limit=100):
        """
        This function applies a test to assure a list of attributes 
        are defined and of the right type.   This function is needed 
        because certain attributes are essential in two different contexts.
        First, for waveform data there are some attributes that are 
        required to construct the data object (e.g. sample interal or 
        sample rate, start time, etc.).  Secondly, workflows generally 
        require certain Metadata and what is required depends upon the 
        workflow.  For example, any work with sources normally requires
        information about both station and instrument properties as well 
        as source.  The opposite is noise correlation work where only 
        station information is essential.  

        :param db:  mspass Database handle.   Note this must be 
        a Database class defined by "from mspasspy.db.database import Database".
        The reason is that the mspass handle is an extension of MongoDB's 
        handle that includes the Schema class used to run the tests in 
        this function. This arg is required
        :param collection:  MongoDB collection that is to be scanned 
        for errors.  Note with normalized data this function should be 
        run on the appropriate wf collection and all normalization 
        collections the wf collection needs to link to. 
        :param keys:  is a list of strings that are to be checked 
        against the contents of the collection.  Note one of the first 
        things the function does is test for the validity of the keys.  
        If they are not defined in the schema the function will throw 
        a MsPASSError exception. 
        :param query:  optional dict passed as a query to limit the 
        documents scanned by the function.   Default will process the 
        entire collection requested.
        :param verbose:  when True errors will be printed.   The default is
        False and the function will do it's work silently.   Verbose is 
        most useful in an interactive python session where the function 
        is called directly.  Most users will run this function 
        as part of tests driven by the dbverify program. 
        :param error_limit: Is a sanity check the number of errors logged
        The number of any type are limited to this number (default 1000).
        The idea is errors should be rare and if this number is exceeded 
        you have a big problem you need to fix before scanning again.  
        The number should be large enough to catch all condition but 
        not so huge it become cumbersome.  With no limit or a memory 
        fault is even possible on a huge dataset.
        :return:  tuple with two components. Both components contain a 
        python dict container keyed by ObjectId of problem documents. 
        The values in the component 0 dict are themselves python dict
        containers that are like MongoDB subdocuments).  The key-value
        pairs in that dict are required data with a type mismatch with the schema.
        The values in component 1 are python lists of keys that had 
        no assigned value but were defined as required.   
        """
        if len(keys)==0:
            raise MsPASSError('check_required:  list of required keys is empty '
                            + '- nothing to test','Fatal')
        # The following two can throw MsPASS errors but we let them 
        # do so. Callers should have a handler for MsPASSError
        dbschema=self.database_schema
        # This holds the schema for the collection to be scanned
        # dbschema is mostly an index to one of these
        col_schema=dbschema[collection]
        dbcol=self[collection]
        # We first make sure the user didn't make a mistake in giving an 
        # invalid key for the required list
        for k in keys:
            if not col_schema.is_defined(k):
                raise MsPASSError('check_required:  schema has no definition for key='
                                + k,'Fatal')

        n=dbcol.count_documents(query)
        if n == 0:
            raise MsPASSError('check_required:  query='
                            +str(query)+' yields zero matching documents',
                            'Fatal')
        undef=dict()
        wrong_types=dict()
        cursor=dbcol.find(query)        
        for doc in cursor:
            id=doc['_id']
            undef_this=list()
            wrong_this=dict()
            for k in keys:
                if not k in doc:
                    undef_this.append(k)
                else:
                    val=doc[k]
                    if type(val)!=col_schema.type(k):
                        wrong_this[k]=val
            if len(undef_this)>0:
                undef[id]=undef_this
            if len(wrong_this)>0:
                wrong_types[id]=wrong_this
            if len(wrong_types)>=error_limit or len(undef)>=error_limit:
                break
        return tuple([wrong_types,undef])

    def update_metadata(self, mspass_object, mode='promiscuous', exclude_keys=[], collection=None, ignore_metadata_changed_test=False):
        """
        Update (or save if it's a new object) the mspasspy object, including saving the processing history, elogs
        and metadata attributes.

        :param mspass_object: the object you want to update.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param mode: reading mode regarding schema checks, should be one of ['promiscuous','cautious','pedantic']
        :type mode: class:`str`
        :param exclude_keys: a list of metadata attributes you want to exclude from being updated.
        :type exclude_keys: a :class:`list` of :class:`str`
        :param collection: the collection name you want to use. If not specified, use the defined collection in the metadata schema.
        :param ignore_metadata_changed_test: if specify as True, we do not check the whether attributes we want to update are in the Metadata.modified() set. Default to be false.
        """
        if not isinstance(mspass_object, (TimeSeries, Seismogram)):
            raise TypeError("only TimeSeries and Seismogram are supported")

        if mode not in ['promiscuous', 'cautious', 'pedantic']:
            raise MsPASSError('only promiscuous, cautious and pedantic are supported, but {} is requested.'.format(mode), 'Fatal')

        has_fatal_error = False
        non_fatal_error_cnt = 0
        if mspass_object.live:
            schema = self.metadata_schema
            if isinstance(mspass_object, TimeSeries):
                update_metadata_def = schema.TimeSeries
            else:
                update_metadata_def = schema.Seismogram

            wf_collection = update_metadata_def.collection('_id') if not collection else collection
            col = self[wf_collection]
            object_doc = None

            new_insertion = False
            if '_id' not in mspass_object:
                new_insertion = True

            if not new_insertion:
                object_doc = col.find_one({'_id': mspass_object['_id']})

            # 1. create the dict of metadata to be saved in wf
            insert_dict = {}

            self._sync_metadata_before_update(mspass_object)
            copied_metadata = Metadata(mspass_object)

            # clear all the aliases
            update_metadata_def.clear_aliases(copied_metadata)

            for k in copied_metadata:
                if not str(copied_metadata[k]).strip():
                    copied_metadata.erase(k)

            for k in copied_metadata:
                # not update the keys in exclude_keys parameter
                if k in exclude_keys:
                    continue

                # only update data marked as modified
                if not ignore_metadata_changed_test and k not in copied_metadata.modified():
                    continue

                # read-only attributes are not supposed to be updated
                if update_metadata_def.is_defined(k) and update_metadata_def.readonly(k):
                    # id could not be updated
                    if k == '_id':
                        continue
                    # normal attribute is read only but can change the attribute to be ERROR_attribute
                    # to prevent dropping error attribute and make original attribute intact
                    mspass_object.elog.log_error('update_metadata',
                            "attribute {} is read only and cannot be updated, but the attribute is saved as READONLYERROR_{}".format(k, k),
                            ErrorSeverity.Informational)
                    non_fatal_error_cnt += 1
                    READONLYERROR_k = "READONLYERROR_" + k
                    copied_metadata.change_key(k, READONLYERROR_k)
                    k = READONLYERROR_k

                # save metadata to wf with blocks for write modes ("promiscuous", "cautious", and "pedantic')
                # promiscuous(no schema check at all)
                if mode == "promiscuous":
                    insert_dict[k] = copied_metadata[k]
                    continue

                # cautious/pedantic(both need schema check)
                if update_metadata_def.is_defined(k):
                    # cautious mode: try to fix the required attributes whose types are mismatch with the schema
                    if mode == "cautious":
                        # try to convert the mismatch metadata attribute
                        if not isinstance(copied_metadata[k], update_metadata_def.type(k)):
                            try:
                                # The following convert the actual value in a dict to a required type.
                                # This is because the return of type() is the class reference.
                                insert_dict[k] = update_metadata_def.type(k)(copied_metadata[k])
                            except Exception as err:
                                # update is not aborted, but mark the mspass object as dead
                                if update_metadata_def.is_required(k):
                                    mspass_object.elog.log_error('update_metadata',
                                        "cautious mode: Required attribute {} has type {}, forbidden by definition and unable to convert".format(k, type(copied_metadata[k])),
                                        ErrorSeverity.Invalid)
                                    has_fatal_error = True
                                    mspass_object.kill()

                        else:
                            # otherwise, we could update this attribute in the metadata
                            insert_dict[k] = copied_metadata[k]
                    
                    # pedantic mode: any type mismatch could end up killing the mspass object
                    elif mode == "pedantic":
                        if not isinstance(copied_metadata[k], update_metadata_def.type(k)):
                            mspass_object.elog.log_error('update_metadata',
                                "pedantic mode: attribute {} has type {}, forbidden by definition".format(k, type(copied_metadata[k])),
                                ErrorSeverity.Invalid)
                            has_fatal_error = True
                            mspass_object.kill()
                        else:
                            # otherwise, we could update this attribute in the metadata
                            insert_dict[k] = copied_metadata[k]

            if mspass_object.live:
                # 2. save/update history
                if not mspass_object.is_empty():
                    history_obj_id_name = self.database_schema.default_name('history_object') + '_id'
                    old_history_object_id = None if new_insertion or history_obj_id_name not in object_doc else object_doc[history_obj_id_name]
                    history_object_id = self._save_history(mspass_object, old_history_object_id)
                    insert_dict.update({history_obj_id_name: history_object_id})

                # 3. save/update error logs
                if mspass_object.elog.size() != 0:
                    elog_id_name = self.database_schema.default_name('elog') + '_id'
                    old_elog_id = None if new_insertion or elog_id_name not in object_doc else object_doc[elog_id_name]
                    elog_id = self._save_elog(mspass_object, old_elog_id)  # elog ids will be updated in the wf col when saving metadata
                    insert_dict.update({elog_id_name: elog_id})
                
                if '_id' not in copied_metadata:  # new_insertion
                    mspass_object['_id'] = col.insert_one(insert_dict).inserted_id
                else:
                    filter_ = {'_id': copied_metadata['_id']}
                    col.update_one(filter_, {'$set': insert_dict})

                # 4. need to save the wf_id back to elog entry if this is an insert
                if new_insertion and mspass_object.elog.size() != 0:
                    elog_col = self[self.database_schema.default_name('elog')]
                    wf_id_name = wf_collection + '_id'
                    filter_ = {'_id': elog_id}
                    elog_col.update_one(filter_, {'$set': {wf_id_name: mspass_object['_id']}})
            else:
                # save the metadata in gravestone as an elog entry
                mspass_object.elog.log_verbose(
                sys._getframe().f_code.co_name, "Skipped updating the metadata of a dead object")
                self._save_elog(mspass_object)
        
        else:
            # FIXME: we could have recorded the full stack here, but need to revise the logger object
            # to make it more powerful for Python logging.
            mspass_object.elog.log_verbose(
                sys._getframe().f_code.co_name, "Skipped updating the metadata of a dead object")
            self._save_elog(mspass_object)
        
        if has_fatal_error:
            return -1
        return non_fatal_error_cnt

    def read_ensemble_data(self, objectid_list, mode='promiscuous', normalize=[], load_history=True, exclude_keys=[], collection='wf'):
        """
        Reads and returns the mspasspy ensemble object stored in the database.

        :param objectid_list: a :class:`list` of :class:`bson.objectid.ObjectId`, where each belongs to a mspasspy object.
        :param load_history: `True` to load object-level history into the mspasspy object.
        :param include_undefined: `True` to also read the attributes in the collection that are not defined in the schema.
        :param exclude_keys: the metadata attributes you want to exclude from being read.
        :type exclude_keys: a :class:`list` of :class:`str`
        :param collection: the collection name in the database that the object is stored. If not specified, use the default wf collection in the schema.
        :return: either :class:`mspasspy.ccore.seismic.TimeSeriesEnsemble` or
            :class:`mspasspy.ccore.seismic.SeismogramEnsemble`.
        """
        wf_collection = self.database_schema.default_name(collection)
        object_type = self.database_schema[wf_collection].data_type()

        if object_type not in [TimeSeries, Seismogram]:
            raise MsPASSError('only TimeSeries and Seismogram are supported, but {} is requested. Please check the data_type of {} collection.'.format(
                object_type, wf_collection), 'Fatal')

        if object_type is TimeSeries:
            ensemble = TimeSeriesEnsemble(len(objectid_list))
        else:
            ensemble = SeismogramEnsemble(len(objectid_list))

        for i in objectid_list:
            ensemble.member.append(self.read_data(i, mode, normalize, load_history, exclude_keys, wf_collection))

        return ensemble

    def save_ensemble_data(self, ensemble_object, mode="promiscuous", storage_mode='gridfs', dfile_list=None, dir_list=None,
                           exclude_keys=[], exclude_objects=[], collection=None):
        """
        Save the mspasspy ensemble object (metadata attributes, processing history, elogs and data) in the mongodb
        database.

        :param ensemble_object: the ensemble you want to save.
        :type ensemble_object: either :class:`mspasspy.ccore.seismic.TimeSeriesEnsemble` or
            :class:`mspasspy.ccore.seismic.SeismogramEnsemble`.
        :param storage_mode: "gridfs" stores the object in the mongodb grid file system (recommended). "file" stores
            the object in a binary file, which requires `dfile` and `dir`.
        :type storage_mode: :class:`str`
        :param dfile_list: A :class:`list` of file names if using "file" storage mode. File name is `str` type.
        :param dir_list: A :class:`list` of file directories if using "file" storage mode. File directory is `str` type.
        :param include_undefined: `True` to also update the metadata attributes not defined in the schema.
        :param exclude_keys: the metadata attributes you want to exclude from being stored.
        :type exclude_keys: a :class:`list` of :class:`str`
        :param exclude_objects: A list of indexes, where each specifies a object in the ensemble you want to exclude from being saved. Starting from 0.
        :type exclude_objects: :class:`list`
        :param collection: the collection name you want to use. If not specified, use the defined collection in the metadata schema.
        """
        if not dfile_list:
            dfile_list = [None for _ in range(len(ensemble_object.member))]
        if not dir_list:
            dir_list = [None for _ in range(len(ensemble_object.member))]

        if storage_mode in ["file", "gridfs"]:
            j = 0
            for i in range(len(ensemble_object.member)):
                if i not in exclude_objects:
                    self.save_data(ensemble_object.member[i], mode, storage_mode, dfile_list[j],
                                   dir_list[j], exclude_keys, collection)
                    j += 1
        elif storage_mode == "url":
            pass
        else:
            raise TypeError("Unknown storage mode: {}".format(storage_mode))

    def update_ensemble_metadata(self, ensemble_object, mode='promiscuous', exclude_keys=[], exclude_objects=[],
                                 collection=None):
        """
        Update (or save if it's new) the mspasspy ensemble object, including saving the processing history, elogs
        and metadata attributes.

        :param ensemble_object: the ensemble you want to update.
        :type ensemble_object: either :class:`mspasspy.ccore.seismic.TimeSeriesEnsemble` or
            :class:`mspasspy.ccore.seismic.SeismogramEnsemble`.
        :param include_undefined: `True` to also update the metadata attributes not defined in the schema.
        :param exclude_keys: the metadata attributes you want to exclude from being updated.
        :type exclude_keys: a :class:`list` of :class:`str`
        :param exclude_objects: a list of indexes, where each specifies a object in the ensemble you want to
        exclude from being saved. The index starts at 0.
        :type exclude_objects: :class:`list`
        :param collection: the collection name you want to use. If not specified, use the defined collection in the metadata
        schema.
        """
        for i in range(len(ensemble_object.member)):
            if i not in exclude_objects:
                self.update_metadata(ensemble_object.member[i], mode, exclude_keys, collection)

    def delete_data(self, object_id, object_type, remove_unreferenced_files=False, clear_history=True, clear_elog=True):
        """
        Delete the wf document by passing mspass object's _id, including deleting the processing history, elogs
        and files/gridfs data the mspass object contains.

        :param object_id: the wf object id you want to delete.
        :type object_id: class:`bson.objectid.ObjectId`
        :param object_type: the object type you want to delete, must be one of ['TimeSeries', 'Seismogram']
        :type object_type: class:`str`
        :param remove_unreferenced_files: if True, we will try to remove the file that no wf data is referencing. Default to be False
        :param clear_history: if True, we will clear the processing history of the associated wf object, default to be True
        :param clear_elog: if True, we will clear the elog entries of the associated wf object, default to be True
        """
        if object_type not in ["TimeSeries", "Seismogram"]:
            raise TypeError("only TimeSeries and Seismogram are supported")

        # get the wf collection name in the schema
        schema = self.metadata_schema
        if object_type is 'TimeSeries':
            detele_schema = schema.TimeSeries
        else:
            detele_schema = schema.Seismogram
        wf_collection_name = detele_schema.collection('_id')
        
        # user might pass a mspass object by mistake
        try:
            oid = object_id['_id']
        except:
            oid = object_id
        
        # fetch the document by the given object id
        object_doc = self[wf_collection_name].find_one({'_id': oid})
        if not object_doc:
            raise MsPASSError('Could not find document in wf collection by _id: {}.'.format(oid), 'Invalid')

        # delete the document just retrieved from the database
        self[wf_collection_name].delete_one({'_id': oid})

        # delete gridfs/file depends on storage mode, and unreferenced files
        storage_mode = object_doc['storage_mode']
        if storage_mode == "gridfs":
            gfsh = gridfs.GridFS(self)
            if gfsh.exists(object_doc['gridfs_id']):
                gfsh.delete(object_doc['gridfs_id'])

        elif storage_mode == "file" and remove_unreferenced_files:
            dir_name = object_doc['dir']
            dfile_name = object_doc['dfile']
            # find if there are any remaining matching documents with dir and dfile
            match_doc_cnt = self[wf_collection_name].count_documents({'dir': dir_name, 'dfile': dfile_name})
            # delete this file
            if match_doc_cnt == 0:
                fname = os.path.join(dir_name, dfile_name)
                os.remove(fname)

        # clear history
        if clear_history:
            history_collection = self.database_schema.default_name('history_object')
            history_obj_id_name = history_collection + '_id'
            if history_obj_id_name in object_doc:
                self[history_collection].delete_one({'_id': object_doc[history_obj_id_name]})
        
        # clear elog
        if clear_elog:
            wf_id_name = wf_collection_name + '_id'
            elog_collection = self.database_schema.default_name('elog')
            elog_id_name = elog_collection + '_id'
            # delete the one by elog_id in mspass object
            if elog_id_name in object_doc:
                self[elog_collection].delete_one({'_id': object_doc[elog_id_name]})
            # delete the documents with the wf_id equals to obejct['_id']
            self[elog_collection].delete_many({wf_id_name: oid})


    def _load_collection_metadata(self,mspass_object, exclude_keys, include_undefined=False, collection=None):
        """
        Master Private Method

        Reads metadata from a requested collection and loads standard attributes from collection to the data passed as mspass_object.
        The method will only work if mspass_object has the collection_id attribute set to link it to a unique document in source.  

        :param mspass_object:   data where the metadata is to be loaded
        :type mspass_object:  must be TimeSeries, Seismogram, TimeSeriesEnsemble, or SeismogramEnsemble.
        :param exclude_keys: list of attributes that should not normally be loaded.
        Default attributes not normally need that are loaded from stationxml.  Ignored if include_undefined is set True.
        :param include_undefined:  when true all data in the matching document are loaded.
        :param collection: requested collection metadata should be loaded
        :type collection: str 

        :exception:  any detected errors will cause a MsPASSError to be thrown
        (colleagues:  this may be wrong sphynx syntax for defining an exception)
        """
        if not mspass_object.live:
            raise MsPASSError("only live mspass object can load metadata", ErrorSeverity.Invalid)

        if not isinstance(mspass_object, (TimeSeries, Seismogram)):
            raise MsPASSError("only TimeSeries and Seismogram are supported", ErrorSeverity.Invalid)

        if collection == 'channel' and isinstance(mspass_object, (Seismogram, SeismogramEnsemble)):
            raise MsPASSError("channel data can not be loaded into Seismogram", ErrorSeverity.Invalid)
        
        # 1. get the metadata schema based on the mspass object type
        if isinstance(mspass_object, TimeSeries):
            metadata_def = self.metadata_schema.TimeSeries
        else:
            metadata_def = self.metadata_schema.Seismogram

        wf_collection = metadata_def.collection('_id')
        object_type = self.database_schema[wf_collection].data_type()
        if object_type not in [TimeSeries, Seismogram]:
            raise MsPASSError('only TimeSeries and Seismogram are supported, but {} is requested. Please check the data_type of {} collection.'.format(
                object_type, wf_collection), 'Fatal')
        wf_collection_metadata_schema = self.metadata_schema[object_type.__name__]

        collection_id = collection + '_id'
        # 2. get the collection_id from the current mspass_object
        if not mspass_object.is_defined(collection_id):
            raise MsPASSError("no {} in the mspass object".format(collection_id), ErrorSeverity.Invalid)
        object_doc_id = mspass_object[collection_id]

        # 3. find the unique document associated with this source id in the source collection
        object_doc = self[collection].find_one({'_id': object_doc_id})
        if object_doc == None:
            raise MsPASSError("no match found in {} collection for source_id = {}".format(collection, object_doc_id), ErrorSeverity.Invalid)
        
        # 4. use this document to update the mspass object
        key_dict = set()
        for k in wf_collection_metadata_schema.keys():
            col = wf_collection_metadata_schema.collection(k)
            if col == collection:
                if k not in exclude_keys and not include_undefined:
                    key_dict.add(self.database_schema[col].unique_name(k))
                    mspass_object.put(k, object_doc[self.database_schema[col].unique_name(k)])

        # 5. add extra keys if include_undefined is true
        if include_undefined:
            for k in object_doc:
                if k not in key_dict:
                    mspass_object.put(k, object_doc[k])


    def load_source_metadata(self,mspass_object, exclude_keys=['serialized_event','magnitude_type'], include_undefined=False):
        """
        Reads metadata from source collection and loads standard attributes in source collection to the data passed as mspass_object.
        The method will only work if mspass_object has the source_id attribute set to link it to a unique document in source.  

        Note the mspass_object can be either an atomic object (TimeSeries or Seismogram) with a Metadata container base class
        or an ensemble (TimeSeriesEnsemble or SeismogramEnsemble).
        Ensembles will have the source data posted to the ensemble Metadata and not the members.
        This should be the stock way to assemble the generalization of a shot gather. 

        :param mspass_object:   data where the source metadata is to be loaded
        :type mspass_object:  must be TimeSeries, Seismogram, TimeSeriesEnsemble, or SeismogramEnsemble.
        :param exclude_keys: list of attributes that should not normally be loaded.
        Default attributes not normally need that are loaded from stationxml.  Ignored if include_undefined is set True.
        :param include_undefined:  when true all data in the matching source document are loaded. 

        :exception:  any detected errors will cause a MsPASSError to be thrown
        (colleagues:  this may be wrong sphynx syntax for defining an exception)
        """
        if isinstance(mspass_object, (TimeSeries, Seismogram)):
            self._load_collection_metadata(mspass_object, exclude_keys, include_undefined, 'source')
        if isinstance(mspass_object, (TimeSeriesEnsemble, SeismogramEnsemble)):
            for member_object in mspass_object.member:
                self._load_collection_metadata(member_object, exclude_keys, include_undefined, 'source')
        

    def load_site_metadata(self,mspass_object, exclude_keys=[], include_undefined=False):
        """
        Reads metadata from site collection and loads standard attributes insite collection to the data passed as mspass_object.
        The method will only work if mspass_object has the site_id attribute set to link it to a unique document in source.  

        Note the mspass_object can be either an atomic object (TimeSeries or Seismogram) with a Metadata container base class or an ensemble (TimeSeriesEnsemble
        or SeismogramEnsemble).
        Ensembles will have the site data posted to the ensemble Metadata and not the members.
        This should be the stock way to assemble the generalization of a common-receiver gather. 

        :param mspass_object:   data where the site metadata is to be loaded
        :type mspass_object:  must be TimeSeries, Seismogram, TimeSeriesEnsemble, or SeismogramEnsemble.
        :param exclude_keys: list of attributes that should not normally be loaded.  Default is none.   Ignored if include_undefined is set True.
        :param include_undefined:  when true all data in the matching source document are loaded 
        :exception:  any detected errors will cause a MsPASSError to be thrown
        (colleagues:  this may be wrong sphynx syntax for defining an exception)
        """
        if isinstance(mspass_object, (TimeSeries, Seismogram)):
            self._load_collection_metadata(mspass_object, exclude_keys, include_undefined, 'site')
        if isinstance(mspass_object, (TimeSeriesEnsemble, SeismogramEnsemble)):
            for member_object in mspass_object.member:
                self._load_collection_metadata(member_object, exclude_keys, include_undefined, 'site')

    def load_channel_metadata(self,mspass_object, exclude_keys=['serialized_channel_data'], include_undefined=False):
        """
        Reads metadata from channel collection and loads standard attributes in channel collection to the data passed as mspass_object.
        The method will only work if mspass_object has the site_id attribute set to link it to a unique document in source.  

        Note the mspass_object can be either an atomic object (TimeSeries or Seismogram) with a Metadata container base class or an ensemble (TimeSeriesEnsemble
        or SeismogramEnsemble).
        Ensembles will have the site data posted to the ensemble Metadata and not the members.
        This should be the stock way to assemble the generalization of a common-receiver gather of TimeSeries data for a common sensor component.

        :param mspass_object:   data where the channel metadata is to be loaded
        :type mspass_object:  must be TimeSeries, Seismogram, TimeSeriesEnsemble, or SeismogramEnsemble.
        :param exclude_keys: list of attributes that should not normally be loaded.
        Default excludes the serialized obspy class that is used to store response data.   Ignored if include_undefined is set True.
        :param include_undefined:  when true all data in the matching source document are loaded 
        
        :exception:  any detected errors will cause a MsPASSError to be thrown
        (colleagues:  this may be wrong sphynx syntax for defining an exception)
        """
        if isinstance(mspass_object, (TimeSeries, Seismogram)):
            self._load_collection_metadata(mspass_object, exclude_keys, include_undefined, 'channel')
        if isinstance(mspass_object, (TimeSeriesEnsemble, SeismogramEnsemble)):
            for member_object in mspass_object.member:
                self._load_collection_metadata(member_object, exclude_keys, include_undefined, 'channel')


    @staticmethod
    def _sync_metadata_before_update(mspass_object):
        """
        MsPASS data objects are designed to cleanly handle what we call relative
        and UTC time.  This small helper function assures the Metadata of
        mspass_object are consistent with the internal contents.  That
        involves posting some special attributes seen below to handle this issue.
        Since Metadata is volatile we need to be sure these are consistent or
        timing can be destroyed on data.
        """
        # this adds a small overhead but it guarantees Metadata and internal t0
        # values are consistent.  Shouldn't happen unless the user messes with them
        # incorrectly, but this safety is prudent to reduce the odds of mysterious
        # timing errors in data
        t0 = mspass_object.t0
        mspass_object.set_t0(t0)
        # This will need to be modified if we ever expand time types beyond two
        if mspass_object.time_is_relative():
            if mspass_object.shifted():
                mspass_object['startime_shift'] = mspass_object.time_reference()
                mspass_object['utc_convertible'] = True
            else:
                mspass_object['utc_convertible'] = False
            mspass_object['time_standard'] = 'Relative'
        else:
            mspass_object['utc_convertible'] = True
            mspass_object['time_standard'] = 'UTC'
            
    def _save_history(self, mspass_object, prev_history_object_id=None, collection=None):
        """
        Save the processing history of a mspasspy object.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param prev_history_object_id: the previous history object id (if it has).
        :type prev_history_object_id: :class:`bson.objectid.ObjectId`
        :param collection: the collection that you want to store the history object. If not specified, use the defined
        collection in the schema.
        :return: current history_object_id.
        """
        if not collection:
            collection = self.database_schema.default_name('history_object')
        history_col = self[collection]
        proc_history = ProcessingHistory(mspass_object)
        current_uuid = proc_history.id() # uuid in the current node
        history_binary = pickle.dumps(proc_history)
        # todo save jobname jobid when global history module is done
        try:
            if prev_history_object_id:
                # overwrite history
                history_col.delete_one({'_id': prev_history_object_id})
                history_col.insert_one({'_id': current_uuid, 'nodedata': history_binary})
            else:
                # new insertion
                history_col.insert_one({'_id': current_uuid, 'nodedata': history_binary})
        except pymongo.errors.DuplicateKeyError as e:
            raise MsPASSError("The history object to be saved has a duplicate uuid", "Fatal") from e

        return current_uuid

    def _load_history(self, mspass_object, history_object_id, collection=None):
        """
        Load (in place) the processing history into a mspasspy object.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param history_object_id: :class:`bson.objectid.ObjectId`
        :param collection: the collection that you want to load the processing history. If not specified, use the defined
        collection in the schema.
        """
        if not collection:
            collection = self.database_schema.default_name('history_object')
        res = self[collection].find_one({'_id': history_object_id})
        mspass_object.load_history(pickle.loads(res['nodedata']))

    def _save_elog(self, mspass_object, elog_id=None, collection=None):
        """
        Save error log for a data object. Data objects in MsPASS contain an error log object used to post any
        errors handled by processing functions. This function will delete the old elog entry if `elog_id` is given.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param elog_id: the previous elog object id to be appended with.
        :type elog_id: :class:`bson.objectid.ObjectId`
        :param collection: the collection that you want to save the elogs. If not specified, use the defined
        collection in the schema.
        :return: updated elog_id.
        """
        if isinstance(mspass_object, TimeSeries):
            update_metadata_def = self.metadata_schema.TimeSeries
        elif isinstance(mspass_object, Seismogram):
            update_metadata_def = self.metadata_schema.Seismogram
        else:
            raise TypeError("only TimeSeries and Seismogram are supported")
        wf_id_name = update_metadata_def.collection('_id') + '_id'

        if not collection:
            collection = self.database_schema.default_name('elog')
        
        #TODO: Need to discuss whether the _id should be linked in a dead elog entry. It 
        # might be confusing to link the dead elog to an alive wf record.
        oid = None
        if '_id' in mspass_object:
            oid = mspass_object['_id']

        elog = mspass_object.elog
        n = elog.size()
        if n != 0:
            logdata = []
            docentry = {'logdata': logdata}
            errs = elog.get_error_log()
            jobid = elog.get_job_id()
            for x in errs:
                logdata.append({'job_id': jobid, 'algorithm': x.algorithm, 'badness': str(x.badness),
                            'error_message': x.message, 'process_id': x.p_id})
            if oid:
                docentry[wf_id_name] = oid

            if not mspass_object.live:
                docentry['gravestone'] = dict(mspass_object)

            if elog_id:
                # append elog
                elog_doc = self[collection].find_one({'_id': elog_id})
                # only append when previous elog exists
                if elog_doc:
                    # if the same object was updated twice, the elog entry will be duplicated
                    # the following list comprehension line removes the duplicates and preserves
                    # the order. May need some practice to see if such a behavior makes sense.
                    [elog_doc['logdata'].append(x) for x in logdata if x not in elog_doc['logdata']]
                    docentry['logdata'] = elog_doc['logdata']
                    self[collection].delete_one({'_id': elog_id})
                # note that is should be impossible for the old elog to have gravestone entry
                # so we ignore the handling of that attribute here.
                ret_elog_id = self[collection].insert_one(docentry).inserted_id
            else:
                # new insertion
                ret_elog_id = self[collection].insert_one(docentry).inserted_id
            return ret_elog_id


    @staticmethod
    def _read_data_from_dfile(mspass_object, dir, dfile, foff):
        """
        Read the stored data from a file and loads it into a mspasspy object.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param dir: file directory.
        :type dir: :class:`str`
        :param dfile: file name.
        :type dfile: :class:`str`
        :param foff: offset that marks the starting of the data in the file.
        """
        fname = os.path.join(dir, dfile)
        with open(fname, mode='rb') as fh:
            fh.seek(foff)
            float_array = array('d')
            if isinstance(mspass_object, TimeSeries):
                if not mspass_object.is_defined('npts'):
                    raise KeyError("npts is not defined")
                float_array.frombytes(fh.read(mspass_object.get('npts') * 8))
                mspass_object.data = DoubleVector(float_array)
            elif isinstance(mspass_object, Seismogram):
                if not mspass_object.is_defined('npts'):
                    raise KeyError("npts is not defined")
                float_array.frombytes(fh.read(mspass_object.get('npts') * 8 * 3))
                print(len(float_array))
                mspass_object.data = dmatrix(3, mspass_object.get('npts'))
                for i in range(3):
                    for j in range(mspass_object.get('npts')):
                        mspass_object.data[i, j] = float_array[i * mspass_object.get('npts') + j]
            else:
                raise TypeError("only TimeSeries and Seismogram are supported")

    @staticmethod
    def _save_data_to_dfile(mspass_object, dir, dfile):
        """
        Saves sample data as a binary dump of the sample data. Save a mspasspy object as a pure binary dump of
        the sample data in native (Fortran) order. Opens the file and ALWAYS appends data to the end of the file.

        This method is subject to several issues to beware of before using them:
        (1) they are subject to damage by other processes/program, (2) updates are nearly impossible without
        stranding (potentially large quantities) of data in the middle of files or
        corrupting a file with a careless insert, and (3) when the number of files
        gets large managing them becomes difficult.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param dir: file directory.
        :type dir: :class:`str`
        :param dfile: file name.
        :type dfile: :class:`str`
        :return: Position of first data sample (foff).
        """
        fname = os.path.join(dir, dfile)
        os.makedirs(os.path.dirname(fname), exist_ok=True)
        with open(fname, mode='a+b') as fh:
            foff = fh.seek(0, 2)
            if isinstance(mspass_object, TimeSeries):
                ub = bytes(np.array(mspass_object.data))  # fixme DoubleVector
            elif isinstance(mspass_object, Seismogram):
                ub = bytes(mspass_object.data)
            else:
                raise TypeError("only TimeSeries and Seismogram are supported")
            fh.write(ub)
        return foff

    def _save_data_to_gridfs(self, mspass_object, gridfs_id=None):
        """
        Save a mspasspy object sample data to MongoDB grid file system. We recommend to use this method
        for saving a mspasspy object inside MongoDB.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param gridfs_id: if the data is already stored and you want to update it, you should provide the object id
        of the previous data, which will be deleted. A new document will be inserted instead.
        :type gridfs_id: :class:`bson.objectid.ObjectId`.
        :return inserted gridfs object id.
        """
        gfsh = gridfs.GridFS(self)
        if gridfs_id and gfsh.exists(gridfs_id):
            gfsh.delete(gridfs_id)
        if isinstance(mspass_object, Seismogram):
            ub = bytes(mspass_object.data)
        else:
            ub = bytes(np.array(mspass_object.data))
        return gfsh.put(pickle.dumps(ub))

    def _read_data_from_gridfs(self, mspass_object, gridfs_id):
        """
        Read data stored in gridfs and load it into a mspasspy object.

        :param mspass_object: the target object.
        :type mspass_object: either :class:`mspasspy.ccore.seismic.TimeSeries` or :class:`mspasspy.ccore.seismic.Seismogram`
        :param gridfs_id: the object id of the data stored in gridfs.
        :type gridfs_id: :class:`bson.objectid.ObjectId`
        """
        gfsh = gridfs.GridFS(self)
        fh = gfsh.get(file_id=gridfs_id)
        ub = pickle.load(fh)
        fmt = "@%dd" % int(len(ub) / 8)
        x = struct.unpack(fmt, ub)
        if isinstance(mspass_object, TimeSeries):
            mspass_object.data = DoubleVector(x)
        elif isinstance(mspass_object, Seismogram):
            if not mspass_object.is_defined('npts'):
                raise KeyError("npts is not defined")
            if len(x) != (3 * mspass_object['npts']):
                emess = "Size mismatch in sample data. Number of points in gridfs file = %d but expected %d" \
                        % (len(x), (3 * mspass_object['npts']))
                raise ValueError(emess)
            mspass_object.data = dmatrix(3, mspass_object['npts'])
            for i in range(3):
                for j in range(mspass_object['npts']):
                    mspass_object.data[i, j] = x[i * mspass_object['npts'] + j]
        else:
            raise TypeError("only TimeSeries and Seismogram are supported")

    @staticmethod
    def _extract_locdata(chanlist):
        """
        Parses the list returned by obspy channels attribute
        for a Station object and returns a dict of unique
        edepth values keyed by loc code.  This algorithm
        would be horribly inefficient for large lists with
        many duplicates, but the assumption here is the list
        will always be small
        """
        alllocs = {}
        for chan in chanlist:
            alllocs[chan.location_code] = [
                chan.start_date,
                chan.end_date,
                chan.latitude,
                chan.longitude,
                chan.elevation,
                chan.depth]
        return alllocs

    def _site_is_not_in_db(self, record_to_test):
        """
        Small helper functoin for save_inventory.
        Tests if dict content of record_to_test is
        in the site collection.  Inverted logic in one sense
        as it returns true when the record is not yet in
        the database.  Uses key of net,sta,loc,starttime
        and endtime.  All tests are simple equality.
        Should be ok for times as stationxml uses nearest
        day as in css3.0.

        originally tried to do the time interval tests with a
        query, but found it was a bit cumbersone to say the least.
        Because this particular query is never expected to return
        a large number of documents we resort to a linear
        search through all matches on net,sta,loc rather than
        using a confusing and ugly query construct.
        """
        dbsite = self.site
        queryrecord = {}
        queryrecord['net'] = record_to_test['net']
        queryrecord['sta'] = record_to_test['sta']
        queryrecord['loc'] = record_to_test['loc']
        matches = dbsite.find(queryrecord)
        # this returns a warning that count is depricated but
        # I'm getting confusing results from google search on the
        # topic so will use this for now
        nrec = matches.count()
        if (nrec <= 0):
            return True
        else:
            # Now do the linear search on time for a match
            st0 = record_to_test['starttime']
            et0 = record_to_test['endtime']
            time_fudge_factor = 10.0
            stp = st0 + time_fudge_factor
            stm = st0 - time_fudge_factor
            etp = et0 + time_fudge_factor
            etm = et0 - time_fudge_factor
            for x in matches:
                sttest = x['starttime']
                ettest = x['endtime']
                if (sttest > stm and sttest < stp and ettest > etm and ettest < etp):
                    return False
            return True

    def _channel_is_not_in_db(self, record_to_test):
        """
        Small helper functoin for save_inventory.
        Tests if dict content of record_to_test is
        in the site collection.  Inverted logic in one sense
        as it returns true when the record is not yet in
        the database.  Uses key of net,sta,loc,starttime
        and endtime.  All tests are simple equality.
        Should be ok for times as stationxml uses nearest
        day as in css3.0.
        """
        dbchannel = self.channel
        queryrecord = {}
        queryrecord['net'] = record_to_test['net']
        queryrecord['sta'] = record_to_test['sta']
        queryrecord['loc'] = record_to_test['loc']
        queryrecord['chan'] = record_to_test['chan']
        matches = dbchannel.find(queryrecord)
        # this returns a warning that count is depricated but
        # I'm getting confusing results from google search on the
        # topic so will use this for now
        nrec = matches.count()
        if (nrec <= 0):
            return True
        else:
            # Now do the linear search on time for a match
            st0 = record_to_test['starttime']
            et0 = record_to_test['endtime']
            time_fudge_factor = 10.0
            stp = st0 + time_fudge_factor
            stm = st0 - time_fudge_factor
            etp = et0 + time_fudge_factor
            etm = et0 - time_fudge_factor
            for x in matches:
                sttest = x['starttime']
                ettest = x['endtime']
                if (sttest > stm and sttest < stp and ettest > etm and ettest < etp):
                    return False
            return True

    def _handle_null_starttime(self, t):
        if t == None:
            return UTCDateTime(0.0)
        else:
            return t

    def _handle_null_endtime(self, t):
        # This constant is used below to set endtime to a time
        # in the far future if it is null
        DISTANTFUTURE = UTCDateTime(2051, 1, 1, 0, 0)
        if t == None:
            return DISTANTFUTURE
        else:
            return t

    def save_inventory(self, inv,
                       networks_to_exclude=['SY'],
                       verbose=False):
        """
        Saves contents of all components of an obspy inventory
        object to documents in the site and channel collections.
        The site collection is sufficient of Seismogram objects but
        TimeSeries data will often want to be connected to the
        channel collection.   The algorithm used will not add
        duplicates based on the following keys:

        For site:
            net
            sta
            chan
            loc
            starttime::endtime - this check is done cautiously with
              a 10 s fudge factor to avoid the issue of floating point
              equal tests.   Probably overly paranoid since these
              fields are normally rounded to a time at the beginning
              of a utc day, but small cost to pay for stabilty because
              this function is not expected to be run millions of times
              on a huge collection.

        for channels:
            net
            sta
            chan
            loc
            starttime::endtime - same approach as for site with same
               issues - note especially 10 s fudge factor.   This is
               necessary because channel metadata can change more
               frequently than site metadata (e.g. with a sensor
               orientation or sensor swap)

        Finally note the site collection contains full response data
        that can be obtained by extracting the data with the key
        "serialized_inventory" and running pickle loads on the returned
        string.

        A final point of note is that not all Inventory objects are created
        equally.   Inventory objects appear to us to be designed as an image
        of stationxml data.  The problem is that stationxml, like SEED, has to
        support a lot of complexity faced by data centers that end users
        like those using this package do not need or want to know.   The
        point is this method flattens the complexity and aims to reduce the
        result to a set of documents in the site and channel collection
        that can be cross referenced to link the right metadata with all
        waveforms in a dataset.

        :param inv: is the obspy Inventory object of station data to save.
        :networks_to_exclude: should contain a list (or tuple) of
            SEED 2 byte network codes that are to be ignored in
            processing.   Default is SY which is used for synthetics.
            Set to None if if all are to be loaded.
        :verbose:  print informational lines if true.  If false
        works silently)

        :return:  tuple with
          0 - integer number of site documents saved
          1 -integer number of channel documents saved
          2 - number of distinct site (net,sta,loc) items processed
          3 - number of distinct channel items processed
        :rtype: tuple
        """

        # site is a frozen name for the collection here.  Perhaps
        # should be a variable with a default
        # to do: need to change source_id to be a copy of the _id string.

        dbcol = self.site
        dbchannel = self.channel
        n_site_saved = 0
        n_chan_saved = 0
        n_site_processed = 0
        n_chan_processed = 0
        for x in inv:
            # Inventory object I got from webservice download
            # makes the sta variable here a net:sta combination
            # We can get the net code like this
            net = x.code
            # This adds feature to skip data for any net code
            # listed in networks_to_exclude
            if networks_to_exclude != None:
                if net in networks_to_exclude:
                    continue
            # Each x now has a station field, BUT tests I ran
            # say for my example that field has one entry per
            # x.  Hence, we can get sta name like this
            stalist = x.stations
            for station in stalist:
                sta = station.code
                starttime = station.start_date
                endtime = station.end_date
                starttime = self._handle_null_starttime(starttime)
                endtime = self._handle_null_endtime(endtime)
                latitude = station.latitude
                longitude = station.longitude
                # stationxml files seen to put elevation in m. We
                # always use km so need to convert
                elevation = station.elevation / 1000.0
                # an obnoxious property of station xml files obspy is giving me
                # is that the start_dates and end_dates on the net:sta section
                # are not always consistent with the channel data.  In particular
                # loc codes are a problem. So we pull the required metadata from
                # the chans data and will override locations and time ranges
                # in station section with channel data
                chans = station.channels
                locdata = self._extract_locdata(chans)
                # Assume loc code of 0 is same as rest
                # loc=_extract_loc_code(chanlist[0])
                # TODO Delete when sure we don't need to keep the full thing
                #picklestr = pickle.dumps(x)
                all_locs = locdata.keys()
                for loc in all_locs:
                    # If multiple loc codes are present on the second pass
                    # rec will contain the objectid of the document inserted
                    # in the previous pass - an obnoxious property of insert_one
                    # This initialization guarantees an empty container
                    rec = dict()
                    rec['loc'] = loc
                    rec['net'] = net
                    rec['sta'] = sta
                    lkey = loc
                    loc_tuple = locdata[lkey]
                    # We use these attributes linked to loc code rather than
                    # the station data - experience shows they are not
                    # consistent and we should use this set.
                    loc_lat = loc_tuple[2]
                    loc_lon = loc_tuple[3]
                    loc_elev = loc_tuple[4]
                    # for consistency convert this to km too
                    loc_elev = loc_elev / 1000.0
                    loc_edepth = loc_tuple[5]
                    loc_stime = loc_tuple[0]
                    loc_stime = self._handle_null_starttime(loc_stime)
                    loc_etime = loc_tuple[1]
                    loc_etime = self._handle_null_endtime(loc_etime)
                    rec['lat'] = loc_lat
                    rec['lon'] = loc_lon
                    # This is MongoDBs way to set a geographic
                    # point - allows spatial queries.  Note longitude
                    # must be first of the pair
                    rec['coords'] = [loc_lat, loc_lon]
                    rec['elev'] = loc_elev
                    rec['edepth'] = loc_edepth
                    rec['starttime'] = starttime.timestamp
                    rec['endtime'] = endtime.timestamp
                    if latitude != loc_lat or longitude != loc_lon or elevation != loc_elev:
                        print(net, ":", sta, ":", loc,
                          " (Warning):  station section position is not consistent with loc code position")
                        print("Data in loc code section overrides station section")
                        print("Station section coordinates:  ", latitude, longitude, elevation)
                        print("loc code section coordinates:  ", loc_lat, loc_lon, loc_elev)
                    if self._site_is_not_in_db(rec):
                        result=dbcol.insert_one(rec)
                        # Note this sets site_id to an ObjectID for the insertion
                        # We use that to define a duplicate we tag as site_id
                        site_id=result.inserted_id
                        self.site.update_one({'_id':site_id},{'$set':{'site_id' : site_id}})
                        n_site_saved+=1
                        if verbose:
                            print("net:sta:loc=", net, ":", sta, ":", loc,
                              "for time span ", starttime, " to ", endtime,
                              " added to site collection")
                    else:
                        if verbose:
                            print("net:sta:loc=", net, ":", sta, ":", loc,
                              "for time span ", starttime, " to ", endtime,
                              " is already in site collection - ignored")
                    n_site_processed += 1
                    # done with site now handle channel
                    # Because many features are shared we can copy rec
                    # note this has to be a deep copy
                    chanrec = copy.deepcopy(rec)
                    # We don't want this baggage in the channel documents
                    # keep them only in the site collection
                    # del chanrec['serialized_inventory']
                    for chan in chans:
                        chanrec['chan'] = chan.code
                        chanrec['vang'] = chan.dip
                        chanrec['hang'] = chan.azimuth
                        chanrec['edepth'] = chan.depth
                        st = chan.start_date
                        et = chan.end_date
                        # as above be careful of null values for either end of the time range
                        st = self._handle_null_starttime(st)
                        et = self._handle_null_endtime(et)
                        chanrec['starttime'] = st.timestamp
                        chanrec['endtime'] = et.timestamp
                        n_chan_processed += 1
                        if (self._channel_is_not_in_db(chanrec)):
                            picklestr = pickle.dumps(chan)
                            chanrec['serialized_channel_data'] = picklestr
                            result = dbchannel.insert_one(chanrec)
                            # insert_one has an obnoxious behavior in that it
                            # inserts the ObjectId in chanrec.  In this loop
                            # we reuse chanrec so we have to delete the id field
                            # howeveer, we first want to update the record to
                            # have chan_id provide an  alternate key to that id
                            # object_id - that makes this consistent with site
                            # we actually use the return instead of pulling from
                            # chanrec
                            idobj=result.inserted_id
                            dbchannel.update_one({'_id':idobj},
                                             {'$set':{'chan_id' : idobj}})
                            del chanrec['_id']
                            n_chan_saved += 1
                            if verbose:
                                print("net:sta:loc:chan=",
                                  net, ":", sta, ":", loc, ":", chan.code,
                                  "for time span ", st, " to ", et,
                                  " added to channel collection")
                        else:
                            if verbose:
                                print('net:sta:loc:chan=',
                                  net, ":", sta, ":", loc, ":", chan.code,
                                  "for time span ", st, " to ", et,
                                  " already in channel collection - ignored")

        # Tried this to create a geospatial index.   Failing
        # in later debugging for unknown reason.   Decided it
        # should be a done externally anyway as we don't use
        # that feature now - thought of doing so but realized
        # was unnecessary baggage
        # dbcol.create_index(["coords",GEOSPHERE])
        #
        # For now we will always print this summary information
        # For expected use it would be essential information
        #
        print("Database.save_inventory processing summary:")
        print("Number of site records processed=", n_site_processed)
        print("number of site records saved=", n_site_saved)
        print("number of channel records processed=", n_chan_processed)
        print("number of channel records saved=", n_chan_saved)
        return tuple([n_site_saved, n_chan_saved, n_site_processed, n_chan_processed])

    def read_inventory(self, net=None, sta=None, loc=None, time=None):
        """
        Loads an obspy inventory object limited by one or more
        keys.   Default is to load the entire contents of the
        site collection.   Note the load creates an obspy
        inventory object that is returned.  Use load_stations
        to return the raw data used to construct an Inventory.

        :param net:  network name query string.  Can be a single
        unique net code or use MongoDB's expression query
        mechanism (e.g. "{'$gt' : 42}).  Default is all
        :param sta: statoin name query string.  Can be a single
        station name or a MongoDB query expression.
        :param loc:  loc code to select.  Can be a single unique
        location (e.g. '01') or a MongoDB expression query.
        :param time:   limit return to stations with
        startime<time<endtime.  Input is assumed an
        epoch time NOT an obspy UTCDateTime. Use a conversion
        to epoch time if necessary.
        :return:  obspy Inventory of all stations matching the
        query parameters
        :rtype:  obspy Inventory
        """
        dbsite = self.site
        query = {}
        if (net != None):
            query['net'] = net
        if (sta != None):
            query['sta'] = sta
        if (loc != None):
            query['loc'] = loc
        if (time != None):
            query['starttime'] = {"$lt": time}
            query['endtime'] = {"$gt": time}
        matchsize = dbsite.count_documents(query)
        result = Inventory()
        if (matchsize == 0):
            return None
        else:
            stations = dbsite.find(query)
            for s in stations:
                serialized = s['serialized_inventory']
                netw = pickle.loads(serialized)
                # It might be more efficient to build a list of
                # Network objects but here we add them one
                # station at a time.  Note the extend method
                # if poorly documented in obspy
                result.extend([netw])
        return result
    def get_seed_site(self, net, sta, loc='NONE', time=-1.0):
        """
        The site collection is assumed to have a one to one
        mapping of net:sta:loc:starttime - endtime.
        This method uses a restricted query to match the
        keys given and returns a dict of coordinate data;
        lat, lon, elev, edepth.
        The (optional) time arg is used for a range match to find
        period between the site startime and endtime.
        Returns None if there is no match.

        The seed modifier in the name is to emphasize this method is
        for data originating as the SEED format that use net:sta:loc:chan
        as the primary index.

        :param net:  network name to match
        :param sta:  station name to match
        :param loc:   optional loc code to made (empty string ok and common)
        default ignores loc in query.
        :param time: epoch time for requested metadata

        :return: MongoDB doc (dict) matching query
        :rtype:  python dict (document) of result.  None if there is no match.
        """
        dbsite = self.site
        query = {}
        query['net'] = net
        query['sta'] = sta
        if (loc != 'NONE'):
            query['loc'] = loc
        if (time > 0.0):
            query['starttime'] = {"$lt": time}
            query['endtime'] = {"$gt": time}
        matchsize = dbsite.count_documents(query)
        if (matchsize == 0):
            return None
        else:
            stations = dbsite.find(query)
            if (matchsize > 1):
                print("get_seed_site (WARNING):  query=", query)
                print("Returned ", matchsize, " documents - should be exactly one")
                print("Returning first entry found")
            stadoc=dbsite.find_one(query)
            return stadoc

    def get_seed_channel(self, net, sta, chan, loc=None, time=-1.0):
        """
        The channel collection is assumed to have a one to one
        mapping of net:sta:loc:chan:starttime - endtime.
        This method uses a restricted query to match the
        keys given and returns a dict of the document contents
        associated with that key.  Note net, sta, and chan are required
        but loc is optional.

        The optional loc code is handled specially.  The reason is 
        that it is common to have the loc code empty.  In seed data that
        puts two ascii blank characters in the 2 byte packet header 
        position for each miniseed blockette.  With pymongo that 
        can be handled one of three ways that we need to handle gracefully.
        That is, one can either set a literal two blank character 
        string, an empty string (""), or a MongoDB NULL.   To handle 
        that confusion this algorithm first queries for all matches
        without loc defined.  If only one match is found that is 
        returned immediately.  If there are multiple matches we
        search though the list of docs returned for a match to 
        loc being conscious of the null string oddity.  

        The (optional) time arg is used for a range match to find
        period between the site startime and endtime.  If not used
        the first occurence will be returned (usually ill adivsed)
        Returns None if there is no match.  Although the time argument 
        is technically option it usually a bad idea to not include
        a time stamp because most stations saved as seed data have 
        time variable channel metadata.

        :param net:  network name to match
        :param sta:  station name to match
        :param chan:  seed channel code to match
        :param loc:   optional loc code to made (empty string ok and common)
        default ignores loc in query.
        :param time: epoch time for requested metadata

        :return: handle to query return
        :rtype:  MondoDB Cursor object of query result.
        """
        dbchannel = self.channel
        query = {}
        query['net'] = net
        query['sta'] = sta
        query['chan'] = chan
        if loc != None:
            query['loc'] = loc
        
        if (time > 0.0):
            query['starttime'] = {"$lt": time}
            query['endtime'] = {"$gt": time}
        matchsize = dbchannel.count_documents(query)
        if (matchsize == 0):
            return None
        if matchsize==1:
            return dbchannel.find_one(query)
        else:
            # Note we only land here when the above yields multiple matches
            if loc == None:
                # We could get here one of two ways.  There could
                # be multiple loc codes and the user didn't specify 
                # a choice or they wanted the empty string (2 cases).
                # We also have to worry about the case where the 
                # time was not specified but needed. 
                # The complexity below tries to unravel all those possibities
                testquery=query
                testquery['loc']=None
                matchsize=dbchannel.count_documents(testquery)
                if matchsize == 1:
                    return dbchannel.find_one(testquery)
                elif matchsize > 1:
                    if time>0.0:
                        print("get_seed_channel:  multiple matches found for net=",
                          net," sta=",sta," and channel=",chan, " with null loc code\n"
                             "Assuming database problem with duplicate documents in channel collection\n",
                            "Returning first one found")
                        return dbchannel.find_one(testquery)
                    else:
                        raise MsPASSError("get_seed_channel:  "
                            + "query with "+net+":"+sta+":"+chan+" and null loc is ambiguous\n"
                            + "Specify at least time but a loc code if is not truly null",
                            "Fatal")
                else:
                    # we land here if a null match didn't work.  
                    #Try one more recovery with setting loc to an emtpy 
                    # string
                    testquery['loc']=""  
                    matchsize=dbchannel.count_documents(testquery)
                    if matchsize == 1:
                        return dbchannel.find_one(testquery)
                    elif matchsize > 1:
                        if time>0.0:
                            print("get_seed_channel:  multiple matches found for net=",
                               net," sta=",sta," and channel=",chan, " with null loc code tested with empty string\n"
                               "Assuming database problem with duplicate documents in channel collection\n",
                               "Returning first one found")
                            return dbchannel.find_one(testquery)
                        else:
                            raise MsPASSError("get_seed_channel:  "
                              + "recovery query attempt with "+net+":"+sta+":"+chan+" and null loc converted to empty string is ambiguous\n"
                              + "Specify at least time but a loc code if is not truly null",
                              "Fatal")

    def get_response(self, net=None, sta=None, chan=None, loc=None, time=None):
        """
        Returns an obspy Response object for seed channel defined by 
        the standard keys net, sta, chan, and loc and a time stamp.  
        Input time can be a UTCDateTime or an epoch time stored as a float.
        
        :param db:  mspasspy Database handle containing a channel collection
          to be queried
        :param net: seed network code (required)
        :param sta: seed station code (required)
        :param chan:  seed channel code (required)
        :param loc:  seed net code.  If None loc code will not be 
          included in the query.  If loc is anything else it is passed 
          as a literal.  Sometimes loc codes are not defined by in the 
          seed data and are literal two ascii space characters.  If so 
          MongoDB translates those to "".   Use loc="" for that case or 
          provided the station doesn't mix null and other loc codes use None. 
        :param time:  time stamp for which the response is requested.  
          seed metadata has a time range for validity this field is 
          required.   Can be passed as either a UTCDateTime object or 
          a raw epoch time stored as a python float. 
        """
        if sta == None or chan == None or net == None or time == None:
            raise MsPASSError('get_response:  missing one of required arguments:  '
                              + 'net, sta, chan, or time', 'Invalid')
        query = {
            'net': net,
            'sta': sta,
            'chan': chan,
        }
        if loc != None:
            query['loc'] = loc
        else:
            loc = '  '  # set here but not used
        if isinstance(time, UTCDateTime):
            t0 = time.timestamp
        else:
            t0 = time
        query['starttime'] = {'$lt': t0}
        query['endtime'] = {'$gt': t0}
        n = self.channel.count_documents(query)
        if n == 0:
            print('No matching documents found in channel for ',
                  net, ":", sta, ":", "chan", chan, "->", loc, "<-", " at time=",
                  UTCDateTime(t0))
            return None
        elif n > 1:
            print(n, ' matching documents found in channel for ',
                  net, ":", sta, ":", "chan", "->", loc, "<-", " at time=",
                  UTCDateTime(t0))
            print('There should be just one - returning the first one found')
        doc = self.channel.find_one(query)
        s = doc['serialized_channel_data']
        chan = pickle.loads(s)
        return chan.response

    def save_catalog(self, cat, verbose=False):
        """
        Save the contents of an obspy Catalog object to MongoDB
        source collection.  All contents are saved even with
        no checking for existing sources with duplicate
        data.   Like the comparable save method for stations,
        save_inventory, the assumption is pre or post cleanup
        will be preformed if duplicates are a major issue.

        :param cat: is the Catalog object to be saved
        :param verbose: Print informational data if true.
        When false (default) it does it's work silently.

        :return: integer count of number of items saved
        """
        # perhaps should demand db is handle to the source collection
        # but I think the cost of this lookup is tiny
        # to do: need to change source_id to be a copy of the _id string.

        dbcol = self.source
        nevents = 0
        for event in cat:
            # event variable in loop is an Event object from cat
            o = event.preferred_origin()
            m = event.preferred_magnitude()
            picklestr = pickle.dumps(event)
            rec = {}
            # rec['source_id']=source_id
            rec['lat'] = o.latitude
            rec['lon'] = o.longitude
            rec['coords'] = [o.latitude, o.longitude]
            # It appears quakeml puts source depths in meter
            # convert to km
            # also obspy's catalog object seesm to allow depth to be
            # a None so we have to test for that condition to avoid
            # aborts
            if o.depth == None:
                depth = 0.0
            else:
                depth = o.depth / 1000.0
            rec['depth'] = depth
            otime = o.time
            # This attribute of UTCDateTime is the epoch time
            # In mspass we only story time as epoch times
            rec['time']=otime.timestamp
            rec['magnitude']=m.mag
            rec['magnitude_type']=m.magnitude_type
            rec['serialized_event']=picklestr
            result=dbcol.insert_one(rec)
            # the return of an insert_one has the object id of the insertion
            # set as inserted_id.  We save taht as source_id as a more
            # intuitive key that _id
            idobj=result.inserted_id
            dbcol.update_one({'_id':idobj},
                        {'$set':{'source_id' : idobj}})
            nevents += 1
        return nevents

    def load_event(self, source_id):
        """
        Return a bson record of source data matching the unique id
        defined by source_id.   The idea is that magic string would
        be extraced from another document (e.g. in an arrival collection)
        and used to look up the event with which it is associated in
        the source collection.

        This function is a relic and may be depricated.  I originally
        had a different purpose.
        """
        dbsource = self.source
        x = dbsource.find_one({'source_id': source_id})
        return x
