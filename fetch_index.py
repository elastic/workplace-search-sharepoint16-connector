import multiprocessing
import time
import copy
import requests
from sharepoint_utils import print_and_log, encode
from urllib.parse import urljoin
from elastic_enterprise_search import WorkplaceSearch
from checkpointing import Checkpoint
from sharepoint_client import SharePoint
from configuration import Configuration
import logger_manager as log
from usergroup_permissions import Permissions
from datetime import datetime
import os
import json
import csv
from tika.tika import TikaException
from sharepoint_utils import extract
import re
import adapter

IDS_PATH = os.path.join(os.path.dirname(__file__), 'doc_id.json')

logger = log.setup_logging("sharepoint_connector_index")
SITE = "site"
LIST = "list"
ITEM = "item"
SITES = "sites"
LISTS = "lists"
ITEMS = "items"
DOCUMENT_SIZE = 100
DATETIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def check_response(response, error_message, exception_message, param_name):
    """ Checks the response received from sharepoint server
        :param response: response from the sharepoint client
        :param error_message: error message if not getting the response
        :param exception message: exception message
        :param param_name: parameter name whether it is SITES, LISTS OR ITEMS
        Returns:
            response_data: response received from invoking
    """
    if not response:
        if param_name == "attachment":
            logger.info(error_message)
            return None
        else:
            logger.error(error_message)
            return None if param_name == SITES else []
    try:
        response_data = response.json()
        response_data = response_data.get("d", {}).get("results")
        return response_data
    except ValueError as exception:
        logger.exception("%s Error: %s" % (exception_message, exception))
        return None if param_name == SITES else []


class FetchIndex:

    def __init__(self, data, start_time, end_time):
        logger.info("Initializing the Indexing class")
        self.is_error = False
        self.ws_host = data.get("enterprise_search.host_url")
        self.ws_token = data.get("workplace_search.access_token")
        self.ws_source = data.get("workplace_search.source_id")
        self.sharepoint_host = data.get("sharepoint.host_url")
        self.objects = data.get("objects")
        self.site_collections = data.get("sharepoint.site_collections")
        self.enable_permission = data.get("enable_document_permission")
        self.start_time = start_time
        self.end_time = end_time
        self.checkpoint = Checkpoint(logger, data)
        self.sharepoint_client = SharePoint(logger)
        self.permissions = Permissions(logger, self.sharepoint_client)
        self.ws_client = WorkplaceSearch(self.ws_host, http_auth=self.ws_token)
        self.mapping_sheet_path = data.get("sharepoint_workplace_user_mapping")

    def index_document(self, document, success_message, failure_message, param_name):
        """ This method indexes the documents to the workplace.
            :param document: document to be indexed
            :param success_message: success message
            :param failure_message: failure message while indexing the document
            :param param_name: parameter name whether it is SITES, LISTS OR ITEMS
        """
        try:
            if document:
                total_documents_indexed = 0
                document_list = [document[i * DOCUMENT_SIZE:(i + 1) * DOCUMENT_SIZE] for i in range((len(document) + DOCUMENT_SIZE - 1) // DOCUMENT_SIZE)]
                for chunk in document_list:
                    response = self.ws_client.index_documents(
                        http_auth=self.ws_token,
                        content_source_id=self.ws_source,
                        documents=chunk
                    )
                    for each in response['results']:
                        if not each['errors']:
                            total_documents_indexed += 1
            logger.info(success_message)
            logger.info("Total %s indexed to the workplace: %s." % (param_name, total_documents_indexed))
        except Exception as exception:
            logger.exception(
                "%s Error: %s"
                % (failure_message, exception)
            )
            self.is_error = True

    def get_schema_fields(self, document_name):
        """ returns the schema of all the include_fields or exclude_fields specified in the configuration file.
            :param document_name: document name from 'sites', 'lists' or 'items'
            Returns:
                schema: included and excluded fields schema
        """
        fields = self.objects.get(document_name)
        adapter_schema = adapter.DEFAULT_SCHEMA[document_name]
        if fields:
            include_fields = fields.get("include_fields")
            exclude_fields = fields.get("exclude_fields")
            if include_fields:
                adapter_schema = {key: val for key, val in adapter_schema.items() if val in include_fields}
            elif exclude_fields:
                adapter_schema = {key: val for key, val in adapter_schema.items() if val not in exclude_fields}
            adapter_schema["id"] = "GUID" if document_name == ITEMS else "Id"
        return adapter_schema

    def index_sites(self, collection, ids, index):
        """This method fetches sites from a collection and invokes the
            index permission method to get the document level permissions.
            If the fetching is not successful, it logs proper message.
            :param collection: collection name
            :param index: index, boolean value
            Returns:
                document: response of sharepoint GET call, with fields specified in the schema
        """
        rel_url = urljoin(
            self.sharepoint_host, f"/sites/{collection}/_api/web/webs"
        )
        logger.info("Fetching the sites detail from url: %s" % (rel_url))
        query = self.sharepoint_client.get_query(
            self.start_time, self.end_time, SITES)
        response = self.sharepoint_client.get(rel_url, query)

        response_data = check_response(
            response,
            "Could not fetch the sites, url: %s" % (rel_url),
            "Error while parsing the get sites response from url: %s."
            % (rel_url),
            SITES,
        )
        if not response_data:
            logger.info("No sites were created for this interval: start time: %s and end time: %s" % (self.start_time, self.end_time))
            return []
        logger.info(
            "Successfuly fetched and parsed %s sites response from SharePoint" % len(response_data)
        )
        logger.info("Indexing the sites to the Workplace")

        schema = self.get_schema_fields(SITES)
        document = []

        if index:
            for num in range(len(response_data)):
                doc = {'type': SITE}
                # need to convert date to iso else workplace search throws error on date format Invalid field value: Value '2021-09-29T08:13:00' cannot be parsed as a date (RFC 3339)"]}
                response_data[num]['Created'] += 'Z'
                for field, response_field in schema.items():
                    doc[field] = response_data[num].get(response_field, None)
                if self.enable_permission is True:
                    doc["_allow_permissions"] = self.index_permissions(
                        key=SITES, site=response_data[num]['ServerRelativeUrl'])
                document.append(doc)
                ids["sites"].update({doc["id"]: response_data[num]["ServerRelativeUrl"]})

            self.index_document(document,
                                "Successfully indexed the sites to the workplace",
                                "Error while indexing the sites to the workplace.",
                                SITES
                                )
        return response_data

    def get_site_paths(self, collection, ids, response_data=None):
        """Extracts the server relative paths of all the sites present in a
            collection.
            :param collection: collection name
            :param response_data: response data after successfully indexed the documents
            Returns:
                sites: list of site paths
        """
        logger.info("Extracting sites name")
        if not response_data:
            logger.info(
                "Site response is not present. Fetching the list for sites"
            )
            response_data = self.index_sites(
                collection, ids, index=False) or []

        sites = []
        for result in response_data:
            sites.append(result.get("ServerRelativeUrl"))
        return sites

    def index_lists(self, sites, ids, index):
        """This method fetches lists from all sites in a collection and invokes the
            index permission method to get the document level permissions.
            If the fetching is not successful, it logs proper message.
            :param sites: site lists
            :param index: index, boolean value
            Returns:
                document: response of sharepoint GET call, with fields specified in the schema
        """
        logger.info("Fetching lists for all the sites")
        responses = []
        document = []
        if not sites:
            logger.info("No list was created in this interval: start time: %s and end time: %s" % (self.start_time, self.end_time))
            return []
        for site in sites:
            rel_url = urljoin(self.sharepoint_host, f"{site}/_api/web/lists")
            logger.info(
                "Fetching the lists for site: %s from url: %s"
                % (site, rel_url)
            )

            query = self.sharepoint_client.get_query(
                self.start_time, self.end_time, LISTS)
            response = self.sharepoint_client.get(
                rel_url, query)

            response_data = check_response(
                response,
                "Could not fetch the list for site: %s" % (site),
                "Error while parsing the get list response for site: %s from url: %s."
                % (site, rel_url),
                LISTS,
            )
            if not response_data:
                logger.info("No list was created for the site : %s in this interval: start time: %s and end time: %s" % (site, self.start_time, self.end_time))
                continue
            logger.info(
                "Successfuly fetched and parsed %s list response for site: %s from SharePoint"
                % (len(response_data), site)
            )

            base_list_url = urljoin(self.sharepoint_host, f"{site}/Lists/")
            schema_list = self.get_schema_fields(LISTS)

            if index:
                ids["lists"].update({site: {}})
                for num in range(len(response_data)):
                    doc = {'type': LIST}
                    for field, response_field in schema_list.items():
                        doc[field] = response_data[num].get(
                            response_field, None)
                    if self.enable_permission is True:
                        doc["_allow_permissions"] = self.index_permissions(
                            key=LISTS, site=site, list_name=response_data[num]['Title'], list_url=response_data[num]['ParentWebUrl'], itemid=None)
                    doc["url"] = urljoin(base_list_url, re.sub(
                        r'[^ \w+]', '', response_data[num]["Title"]))
                    document.append(doc)
                    ids["lists"][site].update({doc["id"]: response_data[num]["Title"]})
                logger.info(
                    "Indexing the list for site: %s to the Workplace" % (site)
                )

                self.index_document(document,
                                    "Successfully indexed the list for site: %s to the workplace." % (
                                        site),
                                    "Error while indexing the list for site: %s to the workplace." % (
                                        site),
                                    LISTS
                                    )

            responses.append(response_data)
        return responses

    def get_lists_paths(self, collection, ids, sites, response_data=None):
        """Extracts the server relative paths and name of all the lists present in the
            sites of a collection
            :param collection: collection name
            :param sites: site list
            :param response_data: response data
            Returns:
                lists: list of dictionaries, each dictionary is a key-value pair of
                list path and list name
        """
        if not sites:
            sites = self.get_site_paths(collection, ids)
        logger.info('Extracting list name')
        lists = {}
        if not response_data:
            logger.info(
                "List response is not present. Fetching the list for sites"
            )
            response_data = (
                self.index_lists(sites, ids, index=False) or []
            )

        lists = {}
        for response in response_data:
            for result in response:
                lists[result.get("Id")] = [result.get(
                    "ParentWebUrl"), result.get("Title")]
        return lists

    def index_items(self, lists, ids, index):
        """This method fetches items from all the lists in a collection and
            invokes theindex permission method to get the document level permissions.
            If the fetching is not successful, it logs proper message.
            :param lists: document lists
            :param index: index, boolean value
            Returns:
                document: response of sharepoint GET call, with fields specified in the schema
        """
        responses = []
        #  here value is a list of url and title
        logger.info("Fetching all the items for the lists")
        if not lists:
            logger.info("No item was created in this interval: start time: %s and end time: %s" % (self.start_time, self.end_time))
            return []
        for value in lists.values():
            ids["list_items"].update({value[0]: {}})
        for list_content, value in lists.items():
            rel_url = urljoin(
                self.sharepoint_host,
                f"{value[0]}/_api/web/lists/getbytitle('{encode(value[1])}')/items",
            )
            logger.info(
                "Fetching the items for list: %s from url: %s"
                % (value[1], rel_url)
            )

            query = self.sharepoint_client.get_query(
                self.start_time, self.end_time, ITEMS)
            response = self.sharepoint_client.get(rel_url, query)

            response_data = check_response(
                response,
                "Could not fetch the items for list: %s" % (value[1]),
                "Error while parsing the get items response for list: %s from url: %s."
                % (value[1], rel_url),
                ITEMS,
            )
            if not response_data:
                logger.info("No item was created for the list %s in this interval: start time: %s and end time: %s" % (value[1], self.start_time, self.end_time))
                continue
            logger.info(
                "Successfuly fetched and parsed %s listitem response for list: %s from SharePoint"
                % (len(response_data), value[1])
            )

            list_name = re.sub(r'[^ \w+]', '', value[1])
            base_item_url = urljoin(self.sharepoint_host,
                                    f"{value[0]}/Lists/{list_name}/DispForm.aspx?ID=")
            schema_item = self.get_schema_fields(ITEMS)
            document = []

            if index:

                ids["list_items"][value[0]].update({value[1]: []})
                rel_url = urljoin(
                    self.sharepoint_host, f'{value[0]}/_api/web/lists/getbytitle(\'{encode(value[1])}\')/items?$select=Attachments,AttachmentFiles,Title&$expand=AttachmentFiles')

                file_response = self.sharepoint_client.get(rel_url, query='?')
                file_response_data = check_response(file_response, "No attachments were found at url %s in the interval: start time: %s and end time: %s" % (
                    rel_url, self.start_time, self.end_time), "Error while parsing file response for file at url %s." % (rel_url), "attachment")

                for num in range(len(response_data)):
                    doc = {'type': ITEM}
                    if response_data[num].get('Attachments'):
                        file_relative_url = file_response_data[num][
                            'AttachmentFiles']['results'][0]['ServerRelativeUrl']
                        url_s = f"{value[0]}/_api/web/GetFileByServerRelativeUrl(\'{file_relative_url}\')/$value"
                        response = self.sharepoint_client.get(
                            urljoin(self.sharepoint_host, url_s), query='?')
                        if response.status_code == requests.codes.ok:
                            try:
                                doc['body'] = extract(response.content)
                            except TikaException as exception:
                                logger.error('Error while extracting the contents from the attachment, Error %s' % (exception))
                                doc['body'] = {}
                    for field, response_field in schema_item.items():
                        doc[field] = response_data[num].get(
                            response_field, None)
                    if self.enable_permission is True:
                        doc["_allow_permissions"] = self.index_permissions(
                            key=ITEMS, list_name=value[1], list_url=value[0], itemid=str(response_data[num]["Id"]))
                    doc["url"] = base_item_url + str(response_data[num]["Id"])
                    document.append(doc)
                    ids["list_items"][value[0]][value[1]].append(
                        response_data[num].get("GUID"))
                logger.info(
                    "Indexing the listitem for list: %s to the Workplace"
                    % (value[1])
                )

                self.index_document(document,
                                    "Successfully indexed the listitem for list: %s to the workplace" % (
                                        value[1]),
                                    "Error while indexing the listitem for list: %s to the workplace." % (
                                        value[1]),
                                    ITEMS
                                    )

            responses.append(document)
        return responses

    def get_roles(self, key, site, list_url, list_name, itemid):
        """ Checks the permissions and returns the user roles.
            :param key: key, a string value
            :param site: site name to check the permission
            :param list_url: list url to access the list
            :param list_name: list name to check the permission
            :param itemid: item id to check the permission
            Returns:
                roles: user roles
        """
        if key == SITES:
            rel_url = urljoin(self.sharepoint_host, site)
            roles = self.permissions.fetch_users(key, rel_url)

        elif key == LISTS:
            rel_url = urljoin(self.sharepoint_host, list_url)
            roles = self.permissions.fetch_users(
                key, rel_url, title=list_name
            )

        elif key == ITEMS:
            rel_url = urljoin(self.sharepoint_host, list_url)
            roles = self.permissions.fetch_users(
                key, rel_url, title=list_name, id=itemid
            )

        return roles, rel_url

    def workplace_add_permission(self, user_name, permission):
        """This method when invoked would index the permission provided in the paramater
            for the user in paramter user_name
            :param user_name: a string value denoting the username of the user
            :param permission: permission that needs to be provided to the user
        """
        try:
            self.ws_client.add_user_permissions(
                content_source_id=self.ws_source,
                http_auth=self.ws_token,
                user=user_name,
                body={
                    "permissions": [permission]
                },
            )
            logger.info(
                "Successfully indexed the permissions for user %s to the workplace" % (
                    user_name
                )
            )
        except Exception as exception:
            logger.exception(
                "Error while indexing the permissions for user: %s to the workplace. Error: %s" % (
                    user_name, exception
                )
            )
            self.is_error = True
            return []

    def index_permissions(
        self,
        key,
        site=None,
        list_name=None,
        list_url=None,
        itemid=None,
    ):
        """This method when invoked, checks the permission inheritance of each object.
            If the object has unique permissions, the list of users having access to it
            is fetched using sharepoint api else the permission levels of the that object
            is taken same as the permission level of the site collection.
            :param key: key, a string value
            :param site: site name to index the permission for the site
            :param list_name: list name to index the permission for the list
            :param list_url: url of the list
            :param itemid: item id to index the permission for the item
            Returns:
                groups: list of users having access to the given object
        """
        roles, rel_url = self.get_roles(key, site, list_url, list_name, itemid)

        groups = []

        roles = check_response(roles, "Cannot fetch the roles for the given object %s at url %s" % (
            key, rel_url), "Error while parsing response for fetch_users for %s at url %s." % (key, rel_url), "roles")

        rows = {}
        if (os.path.exists(self.mapping_sheet_path) and os.path.getsize(self.mapping_sheet_path) > 0):
            with open(self.mapping_sheet_path) as file:
                csvreader = csv.reader(file)
                for row in csvreader:
                    rows[row[0]] = row[1]
        for role in roles:
            title = role["Member"]["Title"]
            groups.append(title)
            users = role["Member"].get("Users")
            if users:
                users = users.get("results")
                for user in users:
                    user_name = rows.get(user['Title'], user['Title'])
                    self.workplace_add_permission(user_name, title)
            else:
                user_name = rows.get(title, title)
                self.workplace_add_permission(user_name, user_name)
        return groups

    def indexing(self, collection, ids, storage, is_error_shared):
        """This method fetches all the objects from sharepoint server and
            ingests them into the workplace search
            :param collection: collection name
            :param ids: id collection of the all the objects
            :param storage: temporary storage for storing all the documents
            :is_error_shared: list of all the is_error values
        """

        sites = []
        lists = {}
        logger.info(
            "Starting to index all the objects configured in the object field: %s"
            % (str(self.objects))
        )

        for key in self.objects:
            response = None
            if key == SITES:
                response = self.index_sites(collection, ids, index=True)
                sites = self.get_site_paths(collection, ids, response)

            if key == LISTS and not self.is_error:
                if not sites:
                    sites = self.get_site_paths(
                        collection, ids, response_data=None)
                responses = self.index_lists(
                    sites, ids, index=True)

                lists = self.get_lists_paths(
                    collection=collection, ids=ids, sites=sites, response_data=responses
                )
            elif self.is_error:
                self.is_error = False
                continue

            if key == ITEMS and not self.is_error:
                if not lists:
                    lists = self.get_lists_paths(collection, ids, sites)
                self.index_items(lists, ids, index=True)
            elif self.is_error:
                self.is_error = False
                continue

        logger.info(
            "Successfuly fetched all the objects for site collection: %s"
            % (collection)
        )

        logger.info(
            "Saving the checkpoint for the site collection: %s" % (collection)
        )

        is_error_shared.append(self.is_error)

        for obj in ['sites', 'lists', 'list_items']:
            if ids.get(obj):
                prev_ids = storage[obj]
                prev_ids.update(ids[obj])
                storage[obj] = prev_ids


def datetime_partitioning(start_time, end_time, processes):
    """ Divides the timerange in equal partitions by number of processors
        :param start_time: start time of the interval
        :param end_time: end time of the interval
        :param processes: number of processors the device have
    """
    start_time = datetime.strptime(start_time, DATETIME_FORMAT)
    end_time = datetime.strptime(end_time, DATETIME_FORMAT)

    diff = (end_time - start_time) / processes
    for idx in range(processes):
        yield (start_time + diff * idx)
    yield end_time


def start(indexing_type):
    """Runs the indexing logic regularly after a given interval
        or puts the connector to sleep
        :param indexing_type: The type of the indexing i.e. Incremental Sync or Full sync
    """
    logger.info("Starting the indexing..")
    config = Configuration("sharepoint_connector_config.yml", logger)

    is_error_shared = multiprocessing.Manager().list()

    if not config.validate():
        print_and_log(
            logger,
            "error",
            "Terminating the indexing as the configuration parameters are not valid",
        )
        exit(0)
    data = config.reload_configs()
    while True:
        current_time = (datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ")
        ids_collection = {"global_keys": {}}
        storage_with_collection = {"global_keys": {}, "delete_keys": {}}

        if (os.path.exists(IDS_PATH) and os.path.getsize(IDS_PATH) > 0):
            with open(IDS_PATH) as ids_store:
                try:
                    ids_collection = json.load(ids_store)
                except ValueError as exception:
                    logger.exception(
                        "Error while parsing the json file of the ids store from path: %s. Error: %s"
                        % (IDS_PATH, exception)
                    )
            
            # delete all the permissions present in workplace search
            if indexing_type == "full_sync":
                sharepoint_client = SharePoint(logger)
                permission = Permissions(logger, sharepoint_client)
                permission.remove_all_permissions(data=data)

        storage_with_collection["delete_keys"] = copy.deepcopy(ids_collection.get("global_keys"))

        for collection in data.get("sharepoint.site_collections"):
            storage = multiprocessing.Manager().dict({"sites": {}, "lists": {}, "list_items": {}})
            logger.info(
                "Starting the data fetching for site collection: %s"
                % (collection)
            )
            check = Checkpoint(logger, data)

            # get the processors from the config file and if not exists, then directly fetch from os
            worker_process = data.get("worker_process")
            if worker_process is None or worker_process <= 0:
                worker_process = os.cpu_count()
            if indexing_type == "incremental":
                start_time, end_time = check.get_checkpoint(
                    collection, current_time)
            else:
                start_time = data.get("start_time")	
                end_time = current_time

            # partitioning the data collection timeframe in equal parts by worker processes
            partitions = list(datetime_partitioning(
                start_time, end_time, worker_process))

            datelist = []
            for sub in partitions:
                datelist.append(sub.strftime(DATETIME_FORMAT))

            jobs = []
            if not ids_collection["global_keys"].get(collection):
                ids_collection["global_keys"][collection] = {
                    "sites": {}, "lists": {}, "list_items": {}}

            for num in range(0, worker_process):
                start_time_partition = datelist[num]
                end_time_partition = datelist[num + 1]

                logger.info(
                    "Successfully fetched the checkpoint details: start_time: %s and end_time: %s, calling the indexing"
                    % (start_time_partition, end_time_partition)
                )
                indexer = FetchIndex(
                    data, start_time_partition, end_time_partition)

                process = multiprocessing.Process(target=indexer.indexing, args=(collection, ids_collection["global_keys"][collection], storage, is_error_shared, ))

                jobs.append(process)

            for job in jobs:
                job.start()
            for job in jobs:
                job.join()

            storage_with_collection["global_keys"][collection] = storage.copy()

            if "True" in is_error_shared:
                check.set_checkpoint(collection, start_time, indexing_type)
            else:
                check.set_checkpoint(collection, end_time, indexing_type)

        with open(IDS_PATH, "w") as f:
            try:
                json.dump(storage_with_collection, f, indent=4)
            except ValueError as exception:
                logger.warn(
                    'Error while adding ids to json file. Error: %s' % (exception))
        if indexing_type == "incremental":
            interval = data.get("indexing_interval")
        else:
            interval = data.get("full_sync_interval")
        # TODO: need to use schedule instead of time.sleep
        logger.info("Sleeping..")
        time.sleep(interval * 60)


if __name__ == "__main__":
    start("incremental")
