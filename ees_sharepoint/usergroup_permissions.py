#
# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one
# or more contributor license agreements. Licensed under the Elastic License 2.0;
# you may not use this file except in compliance with the Elastic License 2.0.
#
"""usergroup_permissions module allows to manage user permissions.

It can be used to fetch user permissions from Sharepoint Server
or clean permissions in Elastic Enterprise Search"""

from elastic_enterprise_search import WorkplaceSearch

from .util import logger

SITES = "sites"
LISTS = "lists"
LIST_ITEMS = "list_items"
DRIVE_ITEMS = "drive_items"


class Permissions:
    """This class encapsulates all module logic."""
    def __init__(self, sharepoint_client):
        self.sharepoint_client = sharepoint_client

    def fetch_users(self, key, rel_url, list_id="", item_id=""):
        """ Invokes GET calls to fetch unique permissions assigned to an object
            :param key: object key
            :param rel_url: relative url to the sharepoint farm
            :param list_id: list guid
            :param item_id: item id
            Returns:
                Response of the GET call
        """
        logger.info("Fetching the user roles for key: %s" % (key))
        maps = {
            SITES: "_api/web/roleassignments?$expand=Member/users,RoleDefinitionBindings",
            LISTS: f"_api/web/lists(guid\'{list_id}\')/roleassignments?$expand=Member/users,RoleDefinitionBindings",
            LIST_ITEMS: f"_api/web/lists(guid\'{list_id}\')/items({item_id})/roleassignments?$expand=Member/users,RoleDefinitionBindings",
            DRIVE_ITEMS: f"_api/web/lists(guid\'{list_id}\')/items({item_id})/roleassignments?$expand=Member/users,RoleDefinitionBindings"
        }
        if not rel_url.endswith("/"):
            rel_url = rel_url + "/"
        return self.sharepoint_client.get(rel_url, maps[key], "permission_users")

    def remove_all_permissions(self, data):
        """ Removes all the permissions present in the workplace
            :param data: configuration data
        """
        ws_host = data.get("enterprise_search.host_url")
        ws_token = data.get("workplace_search.access_token")
        ws_source = data.get("workplace_search.source_id")
        ws_client = WorkplaceSearch(ws_host, http_auth=ws_token)
        try:
            user_permission = ws_client.list_permissions(
                content_source_id=ws_source,
                http_auth=ws_token,
            )

            if user_permission:
                logger.info("Removing the permissions from the workplace...")
                permission_list = user_permission['results']
                for permission in permission_list:
                    ws_client.remove_user_permissions(
                        content_source_id=ws_source,
                        http_auth=ws_token,
                        user=permission['user'],
                        body={
                            "permissions": permission['permissions']
                        }
                    )
                logger.info("Successfully removed the permissions from the workplace.")
        except Exception as exception:
            logger.exception("Error while removing the permissions from the workplace. Error: %s" % exception)

    def fetch_groups(self, rel_url, userid):
        """ Invokes GET calls to fetch the group roles for a user
            :param rel_url: relative url to the sharepoint farm
            :param userid: user id for fetching the roles
        """
        logger.info("Fetching the group roles for userid: %s" % (userid))
        return self.sharepoint_client.get(
            rel_url, f"_api/web/GetUserById({userid})/groups", "permission_groups")
