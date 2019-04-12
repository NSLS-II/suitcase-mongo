import event_model
from ._version import get_versions
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pymongo import UpdateOne, WriteConcern
from threading import Event
import time
import queue
import bson

__version__ = get_versions()['version']
del get_versions


class Serializer(event_model.DocumentRouter):
    """
    Insert bluesky documents into MongoDB using an embedded data model.

    This embedded data model has three collections: header, event,
    datum. The header collection includes start, stop, descriptor,
    and resource documents. The event_pages are stored in the event colleciton,
    and datum_pages are stored in the datum collection.

    To ensure data integrity two databases are required. A volatile database
    that allows the update of existing documents. And a permanent database that
    does not allow updates to documents. When the stop document is received
    from the RunEngine the volatile data is "frozen" by moving it to the
    permanent database.

    This Serializer ensures that when the stop document or close request is
    received all documents we be written to the volatile database. After
    everything has been successfully writen to volatile database, it will copy
    all of the runs documents to the permanent database, check that it has been
    correctly copied, and then delete the volatile data.

    This Serializer assumes that documents have been previously validated
    according to the bluesky event-model.

    Note that this Seralizer does not share the standard Serializer
    name or signature common to suitcase packages because it can only write
    via pymongo, not to an arbitrary user-provided buffer.

    Examples
    --------
    >>> from bluesky import RunEngine
    >>> from bluesky.plans import scan
    >>> from mongobox import MongoBox
    >>> from ophyd.sim import det, motor
    >>> from suitcase.mongo_embedded import Serializer

    >>> # Create two sandboxed mongo instances
    >>> volatile_box = MongoBox()
    >>> permanent_box = MongoBox()
    >>> volatile_box.start()
    >>> permanent_box.start()

    >>> # Get references to the mongo databases
    >>> volatile_db = volatile_box.client().db
    >>> permanent_db = permanent_box.client().db

    >>> # Create the Serializer
    >>> serializer = Serializer(volatile_db, permanent_db)

    >>> RE = RunEngine({})
    >>> RE.subscribe(serializer)

    >>> # Generate example data.
    >>> RE(scan([det], motor, 1, 10, 10))
    """

    def __init__(self, volatile_db, permanent_db, num_threads=1,
                 queue_size=20, embedder_size=1000000, page_size=5000000,
                 max_insert_time=10, **kwargs):

        """
        Insert documents into MongoDB using an embedded data model.

        Parameters
        ----------
        volatile_db: pymongo database
            database for temporary storage
        permanent_db: pymongo database
            database for permanent storage
        num_theads: int, optional
            number of workers that read from the buffer and write to the
            database. Must be 5 or less. Default is 1.
        queue_size: int, optional
            maximum size of the queue.
        page_size: int, optional
            the document size for event_page and datum_page documents. The
            maximum event/datum_page size is embedder_size + page_size.
            embedder_size + page_size must be less than 15000000.The
            default is 5000000.
        embedder_size: int, optional
            maximum size of the embedder
        max_insert_time: int, optional
            maximum time that the workers will wait before doing a database
            insert.
        """

        # There is no performace improvment for more than 10 threads. Tests
        # validate for upto 10 threads.
        if num_threads > 10 or num_threads < 1:
            raise ValueError(f"num_threads must be between 1 and 10"
                             "inclusive.")

        if page_size < 1000:
            raise ValueError(f"page_size must be >= 1000")

        # Maximum size of a document in mongo is 16MB. buffer_size + page_size
        # defines the biggest document that can be created.
        if embedder_size + page_size > 15000000:
            raise ValueError(f"embedder_size: {embedder_size} + page_size:"
                             "{page_size} is greater then 15000000.")

        self._QUEUE_SIZE = queue_size
        self._EMBED_SIZE = embedder_size
        self._PAGE_SIZE = page_size
        self._MAX_INSERT = max_insert_time
        self._permanent_db = permanent_db
        self._volatile_db = volatile_db
        self._event_queue = queue.Queue(maxsize=self._QUEUE_SIZE)
        self._datum_queue = queue.Queue(maxsize=self._QUEUE_SIZE)
        self._event_embedder = Embedder('event', self._EMBED_SIZE)
        self._datum_embedder = Embedder('datum', self._EMBED_SIZE)
        self._kwargs = kwargs
        self._start_found = False
        self._run_uid = None
        self._frozen = False
        self._count = Event()
        self._worker_error = None
        self._stop_doc = None

        # _event_count and _datum_count are used for setting first/last_index
        # fields of event and datum pages
        self._event_count = defaultdict(lambda: 0)
        self._datum_count = defaultdict(lambda: 0)

        # These counters are used to track the total number of events and datum
        # that have been successfully inserted into the database.
        self._db_event_count = defaultdict(lambda: 0)
        self._db_datum_count = defaultdict(lambda: 0)

        # Start workers.
        self._event_executor = ThreadPoolExecutor(max_workers=1)
        self._datum_executor = ThreadPoolExecutor(max_workers=1)
        self._count_executor = ThreadPoolExecutor(max_workers=1)
        self._event_executor.submit(self._event_worker)
        self._datum_executor.submit(self._datum_worker)
        self._count_executor.submit(self._count_worker)

    def __call__(self, name, doc):
        # Before inserting into mongo, convert any numpy objects into built-in
        # Python types compatible with pymongo.
        sanitized_doc = event_model.sanitize_doc(doc)
        if self._worker_error:
            raise RuntimeError("Worker exception: ") from self._worker_error
        if self._frozen:
            raise RuntimeError("Cannot insert documents into "
                               "frozen Serializer.")

        return super().__call__(name, sanitized_doc)

    def _try_wrapper(f):
        from functools import wraps
        @wraps(f)
        def inner(self):
            try:
                f(self)
            except Exception as error:
                self._worker_error = error
                raise
        return inner

    @_try_wrapper
    def _event_worker(self):
        # Gets events from the queue, embedds them, and writes them to the
        # volatile database.
        last_push = 0
        event = None

        # When a stop document is received 'False' is pushed on to the
        # queue, this signals the worker to finish.
        while event is not False:
            last_push = time.monotonic()
            do_push = False
            try:
                if event is None:
                    event = self._event_queue.get(timeout=0.5)
            except queue.Empty:
                do_push = True
            else:
                # embedder.insert() returns None if the document is inserted,
                # and returns the document, if embedder is full.
                if event is not False:
                    event = self._event_embedder.insert(event)
            if (
                    event is not None
                    or event is False
                    or do_push
                    or time.monotonic() > (last_push + self._MAX_INSERT)):
                if not self._event_embedder.empty():
                    event_dump, dump_sizes = self._event_embedder.dump()
                    self._bulkwrite_event(event_dump, dump_sizes)
                    for descriptor, event_page in event_dump.items():
                        self._db_event_count['count_' + descriptor] += len(
                                event_page['seq_num'])
                last_push = time.monotonic()
                do_push = False

    @_try_wrapper
    def _datum_worker(self):
        # Gets datum from the queue, embedds them, and writes them to the
        # volatile database.

        last_push = 0
        datum = None

        # When a stop document is received 'False' is pushed on to the
        # queue, this signals the worker to finish.
        while datum is not False:
            last_push = time.monotonic()
            do_push = False
            try:
                if datum is None:
                    datum = self._datum_queue.get(timeout=0.5)
            except queue.Empty:
                do_push = True
            else:
                # embedder.insert() returns None if the document is inserted,
                # and returns the document, if embedder is full.
                if datum is not False:
                    datum = self._datum_embedder.insert(datum)
            if (
                    datum is not None
                    or datum is False
                    or do_push
                    or time.monotonic() > (last_push + self._MAX_INSERT)):

                if not self._datum_embedder.empty():
                    datum_dump, dump_sizes = self._datum_embedder.dump()
                    self._bulkwrite_datum(datum_dump. dump_sizes)
                    for resource, datum_page in datum_dump.items():
                        self._db_datum_count['count_' + resource] += len(
                                datum_page['datum_id'])
                last_push = time.monotonic()
                do_push = False

    @_try_wrapper
    def _count_worker(self):
        # Updates event_count and datum_count in the header document

        last_event_count = defaultdict(lambda: 0)
        last_datum_count = defaultdict(lambda: 0)

        while not self._frozen:
            self._count.wait(timeout=5)
            # Only updates the header if the count has changed.
            if (
                    (sum(self._db_event_count.values()) >
                     sum(last_event_count.values()))
                    or (sum(self._db_datum_count.values()) >
                        sum(last_datum_count.values()))
               ):
                self._volatile_db.header.update_one(
                    {'run_id': self._run_uid},
                    {'$set': {**dict(self._db_event_count),
                              **dict(self._db_datum_count)}})

                last_event_count = self._db_event_count
                last_datum_count = self._db_datum_count

    def start(self, doc):
        self._check_start(doc)
        self._run_uid = doc['uid']
        self._insert_header('start', doc)
        self._insert_header('event_count', doc)
        self._insert_header('datum_count', doc)
        return doc

    def stop(self, doc):
        self._stop_doc = doc
        self.close()
        return doc

    def descriptor(self, doc):
        self._insert_header('descriptors', doc)
        return doc

    def resource(self, doc):
        self._insert_header('resources', doc)
        return doc

    def event(self, doc):
        self._event_queue.put(doc)
        return doc

    def datum(self, doc):
        self._datum_queue.put(doc)
        return doc

    def event_page(self, doc):
        self._bulkwrite_event({doc['descriptor']: doc})
        return doc

    def datum_page(self, doc):
        self._bulkwrite_datum({doc['resource']: doc})
        return doc

    def close(self):
        self.freeze(self._run_uid)

    def freeze(self, run_uid):
        """
        Freeze the run by flushing the buffers and moving all of the run's
        documents to the permanent database. This method checks that the data
        has been transfered correcly, and then deletes the volatile data.
        """
        # Freeze the serializer.
        self._frozen = True
        self._event_queue.put(False)
        self._datum_queue.put(False)

        # Interupt the count worker sleep
        self._count.set()

        # No need to wait the 5 seconds for count_executor to finish because we
        # update the final count after explicitly durring freeze.
        self._count_executor.shutdown(wait=True)
        self._event_executor.shutdown(wait=True)
        self._datum_executor.shutdown(wait=True)

        self._set_header('event_count', sum(self._event_count.values()))
        self._set_header('datum_count', sum(self._datum_count.values()))

        if self._worker_error:
            raise RuntimeError("Worker exception: ") from self._worker_error

        # Raise exception if buffers are not empty.
        assert self._event_queue.empty()
        assert self._datum_queue.empty()
        assert self._event_embedder.empty()
        assert self._datum_embedder.empty()

        # Insert the stop doc.
        self._insert_header('stop', self._stop_doc)

        # Copy the run to the permanent database.
        volatile_run = self._get_run(self._volatile_db, run_uid)
        self._insert_run(self._permanent_db, volatile_run)
        permanent_run = self._get_run(self._permanent_db, run_uid)

        # Check that it has been copied correctly to permanent database, then
        # delete the run from the volatile database.
        if volatile_run != permanent_run:
            raise IOError("Failed to move data to permanent database.")
        else:
            self._delete_run(self._volatile_db, run_uid)

    def _delete_run(self, db, run_uid):

        # Get the header.
        header = db.header.find_one({'run_id': run_uid}, {'_id': False})
        if header is None:
            raise RuntimeError(f"Cannot delete run, run not found {run_uid}.")

        # Delete the events.
        if 'descriptors' in header.keys():
            for descriptor in header['descriptors']:
                db.event.remove({'descriptor': descriptor['uid']})

        # Delete the datum.
        if 'resources' in header.keys():
            for resource in header['resources']:
                db.datum.remove({'resource': resource['uid']})

        # Delete the header.
        self._volatile_db.header.remove({'run_id': run_uid})


    def _get_run(self, db, run_uid):
        """
        Gets a run from a database. Returns a list of the run's documents.
        """
        run = list()

        # Get the header.
        header = db.header.find_one({'run_id': run_uid}, {'_id': False})
        if header is None:
            raise RuntimeError(f"Run not found {run_uid} in volatile_db.")

        run.append(('header', header))

        # Get the events.
        if 'descriptors' in header.keys():
            for descriptor in header['descriptors']:
                run += [('event', doc) for doc in
                        db.event.find({'descriptor': descriptor['uid']},
                                      {'_id': False})]

        # Get the datum.
        if 'resources' in header.keys():
            for resource in header['resources']:
                run += [('datum', doc) for doc in
                        db.datum.find({'resource': resource['uid']},
                                      {'_id': False})]
        return run

    def _insert_run(self, db, run):
        """
        Inserts a run into a database. run is a list of the run's documents.
        """
        for collection, doc in run:
            db[collection].insert_one(doc)
            # del doc['_id'] is needed because insert_one mutates doc.
            del doc['_id']

    def _insert_header(self, name,  doc):
        """
        Inserts header document into the run's header document.
        """
        self._volatile_db.header.update_one({'run_id': self._run_uid},
                                            {'$push': {name: doc}},
                                            upsert=True)

    def _set_header(self, name,  doc):
        """ 
        Inserts header document into the run's header document.
        """
        self._volatile_db.header.update_one({'run_id': self._run_uid},
                                            {'$set': {name: doc}})

    def _bulkwrite_datum(self, datum_buffer, dump_sizes):
        """
        Bulk writes datum_pages to Mongo datum collection.
        """
        operations = [self._updateone_datumpage(resource, datum_page,
                                                dump_sizes[resource])
                      for resource, datum_page in datum_buffer.items()]
        self._volatile_db.datum.bulk_write(operations, ordered=False)

    def _bulkwrite_event(self, event_buffer, dump_sizes):
        """
        Bulk writes event_pages to Mongo event collection.
        """
        operations = [self._updateone_eventpage(descriptor, event_page,
                                                dump_sizes[descriptor])
                      for descriptor, event_page in event_buffer.items()]
        self._volatile_db.event.bulk_write(operations, ordered=False)

    def _updateone_eventpage(self, descriptor_id, event_page, size):
        """
        Creates the UpdateOne command that gets used with bulk_write.
        """
        event_size = size

        data_string = {'data.' + key: {'$each': value_array}
                       for key, value_array in event_page['data'].items()}

        timestamp_string = {'timestamps.' + key: {'$each': value_array}
                            for key, value_array
                            in event_page['timestamps'].items()}

        filled_string = {'filled.' + key: {'$each': value_array}
                         for key, value_array in event_page['filled'].items()}

        update_string = {**data_string, **timestamp_string, **filled_string}

        count = len(event_page['seq_num'])
        self._event_count[descriptor_id] += count

        return UpdateOne(
            {'descriptor': descriptor_id, 'size': {'$lt': self._PAGE_SIZE}},
            {'$push': {'uid': {'$each': event_page['uid']},
                       'time': {'$each': event_page['time']},
                       'seq_num': {'$each': event_page['seq_num']},
                       **update_string},
             '$inc': {'size': event_size},
             '$min': {'first_index': self._event_count[descriptor_id] - count},
             '$max': {'last_index': self._event_count[descriptor_id] - 1}},
            upsert=True)

    def _updateone_datumpage(self, resource_id, datum_page, size):
        """
        Creates the UpdateOne command that gets used with bulk_write.
        """
        datum_size = size

        kwargs_string = {'datum_kwargs.' + key: {'$each': value_array}
                         for key, value_array
                         in datum_page['datum_kwargs'].items()}

        count = len(datum_page['datum_id'])
        self._datum_count[resource_id] += count

        return UpdateOne(
            {'resource': resource_id, 'size': {'$lt': self._PAGE_SIZE}},
            {'$push': {'datum_id': {'$each': datum_page['datum_id']},
                       **kwargs_string},
             '$inc': {'size': datum_size},
             '$min': {'first_index': self._datum_count[resource_id] - count},
             '$max': {'last_index': self._datum_count[resource_id] - 1}},
            upsert=True)

    def _check_start(self, doc):
        if self._start_found:
            raise RuntimeError(
                "The serializer in suitcase-mongo expects "
                "documents from one run only. Two `start` documents were "
                "received.")
        else:
            self._start_found = True

    def explicit_freeze(self, run_uid):
        """
        Freeze the run by flushing the buffers and moving all of the run's
        documents to the permanent database. This method is inteded to be used
        if a run fails, and the freeze method is not called. This method can be
        used to freeze a partial run.
        """
        # Freeze the serializer.
        self._frozen = True

        self._event_queue.put(False)
        self._datum_queue.put(False)

        self._count_executor.shutdown(wait=False)
        self._event_executor.shutdown(wait=True)
        self._datum_executor.shutdown(wait=True)

        if self._worker_error:
            raise RuntimeError("Worker exception: ") from self._worker_error

        # Raise exception if buffers are not empty.
        assert self._event_queue.empty()
        assert self._datum_queue.empty()
        assert self._event_embedder.empty()
        assert self._datum_embedder.empty()

        # Copy the run to the permanent database.
        volatile_run = self._get_run(self._volatile_db, run_uid)
        self._insert_run(self._permanent_db, volatile_run)
        permanent_run = self._get_run(self._permanent_db, run_uid)

        # Check that it has been copied correctly to permanent database, then
        # delete the run from the volatile database.
        if volatile_run != permanent_run:
            raise IOError("Failed to move data to permanent database.")
        else:
            self._volatile_db.header.drop()
            self._volatile_db.event.drop()
            self._volatile_db.datum.drop()


class Embedder():

    """
    Embedder embeds normalized bluesky documents.

    "embedding" refers to combining multiple documents from a stream of
    documents into a single document, where the values of matching keys are
    stored as a list, or dictionary of lists. "embedding" converts event docs
    to event_pages, or datum doc to datum_pages. event_pages and datum_pages
    are defined by the bluesky event-model.

    Events with different descriptors, or datum with different resources are
    stored in separate embedded documents. Embedder uses a defaultdict so new
    embedded documents are automatically created when they are needed. The dump
    method returns the embedded dictionary. This mechanism manages the lifetime
    of the event streams in the buffer.

    The doc_type argument which can be either 'event' or 'datum'.
    The the details of the embedding differ for event and datum documents.

    Internally the embedder is a dictionary that maps event decriptors to
    event_pages or datum resources to datum_pages.

    Parameters
    ----------
    doc_type: str
        {'event', 'datum'}
    max_size: int
        Maximum embedder size in bytes.

    Attributes
    ----------
    current_size: int
        Current size of the embedded documents.

    """

    def __init__(self, doc_type, max_size):
        self._embedder = defaultdict(lambda: defaultdict(lambda:
                                                         defaultdict(list)))
        self.current_size = 0
        self.stream_size = defaultdict(lambda: 0)

        if (max_size >= 1000) and (max_size <= 15000000):
            self._max_size = max_size
        else:
            raise ValueError(f"Invalid max_size {max_size}, "
                             "max_size must be between 1000 and "
                             "15000000 inclusive.")

        # Event docs and datum docs are embedded differently, this configures
        # the buffer for the specified document type.
        if doc_type == "event":
            self._array_keys = set(["seq_num", "time", "uid"])
            self._dataframe_keys = set(["data", "timestamps", "filled"])
            self._stream_id_key = "descriptor"
        elif doc_type == "datum":
            self._array_keys = set(["datum_id"])
            self._dataframe_keys = set(["datum_kwargs"])
            self._stream_id_key = "resource"
        else:
            raise ValueError(f"Invalid doc_type {doc_type}, doc_type must "
                             "be either 'event' or 'datum'")

    def dump(self):
        """
        Get everything that has been embedded  and clear the buffer.

        Returns
        -------
        embedder_dump: dict
            A dictionary that maps event descriptor to event_page, or a
            dictionary that maps datum resource to datum_page.
        """
        # Get a reference to the current dict, create a new dict.
        embedder_dump = self._embedder
        self._embedder = defaultdict(lambda: defaultdict(lambda:
                                                         defaultdict(list)))
        dump_sizes = dict(self.stream_size)
        self.stream_size = defaultdict(lambda: 0)
        self.current_size = 0
        return embedder_dump, dump_sizes

    def insert(self, doc):
        """
        Embeds a bluesky event or datum document.
        Parameters
        ----------
        doc: json
            A validated bluesky event or datum document.
        Returns
        -------
        result: bool
            True if insert is successful, False if it failed.
        """
        doc_size = len(bson.BSON.encode(doc))
        if (self.current_size + doc_size) > self._max_size:
            return doc

        for key, value in doc.items():
            if key in self._array_keys:
                self._embedder[doc[self._stream_id_key]][key] = list(
                    self._embedder[doc[self._stream_id_key]][key])
                self._embedder[doc[self._stream_id_key]][key].append(value)
            elif key in self._dataframe_keys:
                for inner_key, inner_value in doc[key].items():
                    (self._embedder[doc[self._stream_id_key]][key]
                        [inner_key].append(inner_value))
            else:
                self._embedder[doc[self._stream_id_key]][key] = value

        self.current_size += doc_size
        self.stream_size[doc[self._stream_id_key]] += doc_size
        return None

    def empty(self):
        return not self.current_size
