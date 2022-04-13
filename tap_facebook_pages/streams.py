"""Stream class for tap-facebook-pages."""
import time as t
import datetime
import re
import sys
import copy
from pathlib import Path
from typing import Any, Dict, Optional, Iterable, cast
from singer_sdk.streams import RESTStream

import singer
from singer import metadata

import urllib.parse
import requests
import logging

logger = logging.getLogger("tap-facebook-pages")
logger_handler = logging.StreamHandler(stream=sys.stderr)
logger.addHandler(logger_handler)
logger.setLevel("INFO")
logger_handler.setFormatter(logging.Formatter('%(levelname)s %(message)s'))

NEXT_FACEBOOK_PAGE = "NEXT_FACEBOOK_PAGE"

SCHEMAS_DIR = Path(__file__).parent / Path("./schemas")

BASE_URL = "https://graph.facebook.com/v10.0/{page_id}"


class FacebookPagesStream(RESTStream):
    access_tokens = {}
    metrics = []
    partitions = []
    page_id: str

    def request_records(self, partition: Optional[dict]) -> Iterable[dict]:
        """Request records from REST endpoint(s), returning response records.

        If pagination is detected, pages will be recursed automatically.
        """
        self.logger.info("Reading data for {}".format(partition and partition.get("page_id", False)))

        next_page_token: Any = None
        finished = False
        while not finished:
            prepared_request = self.prepare_request(
                partition, next_page_token=next_page_token
            )
            try:
                resp = self._request_with_backoff(prepared_request)
                for row in self.parse_response(resp):
                    yield row
                previous_token = copy.deepcopy(next_page_token)
                next_page_token = self.get_next_page_token(
                    response=resp, previous_token=previous_token
                )
                if next_page_token and next_page_token == previous_token:
                    raise RuntimeError(
                        f"Loop detected in pagination. "
                        f"Pagination token {next_page_token} is identical to prior token."
                    )
                # Cycle until get_next_page_token() no longer returns a value
                finished = not next_page_token
            except Exception as e:
                self.logger.warning(e)
                finished = not next_page_token

    def prepare_request(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> requests.PreparedRequest:
        req = super().prepare_request(partition, next_page_token)
        self.logger.info(re.sub("access_token=[a-zA-Z0-9]+&", "access_token=*****&", urllib.parse.unquote(req.url)))
        return req

    @property
    def url_base(self) -> str:
        return BASE_URL

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:
        self.page_id = partition["page_id"]
        if next_page_token:
            return urllib.parse.parse_qs(urllib.parse.urlparse(next_page_token).query)

        params = {}

        starting_datetime = self.get_starting_timestamp(partition)
        if starting_datetime:
            start_date_timestamp = int(starting_datetime.timestamp())
            params.update({"since": start_date_timestamp})

        if partition["page_id"] in self.access_tokens:
            params.update({"access_token": self.access_tokens[partition["page_id"]]})
        else:
            self.logger.info("Not enough rights for page: " + partition["page_id"])

        params.update({"limit": 100})
        return params

    def get_next_page_token(self, response: requests.Response, previous_token: Optional[Any] = None) -> Any:
        resp_json = response.json()
        if "paging" in resp_json and "next" in resp_json["paging"]:
            return resp_json["paging"]["next"]
        return None

    def post_process(self, row: dict, partition: dict) -> dict:
        if "page_id" in partition:
            row["page_id"] = partition["page_id"]
        return row

    @property
    def _singer_metadata(self) -> dict:
        """Return metadata object (dict) as specified in the Singer spec.

        Metadata from an input catalog will override standard metadata.
        """
        if self._tap_input_catalog:
            catalog = singer.Catalog.from_dict(self._tap_input_catalog)
            catalog_entry = catalog.get_stream(self.tap_stream_id)
            if catalog_entry:
                return cast(dict, catalog_entry.metadata)

        # Fix replication method to pass state
        md = cast(
            dict,
            metadata.get_standard_metadata(
                schema=self.schema,
                replication_method=self.replication_method,
                key_properties=self.primary_keys or None,
                valid_replication_keys=(
                    [self.replication_key] if self.replication_key else None
                ),
                schema_name=None,
            ),
        )
        return md

    def get_stream_or_partition_state(self, partition: Optional[dict]) -> dict:
        """Return partition state if applicable; else return stream state."""
        state = self.stream_state
        if partition:
            state = self.get_partition_state(partition)

        if "progress_markers" in state and isinstance(state.get("progress_markers", False), list):
            state["progress_markers"] = {}
        return state


class Page(FacebookPagesStream):
    name = "page"
    tap_stream_id = "page"
    path = ""
    primary_keys = ["id"]
    replication_key = None
    forced_replication_method = "FULL_TABLE"
    schema_filepath = SCHEMAS_DIR / "page.json"

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:
        params = super().get_url_params(partition, next_page_token)
        fields = ','.join(self.config['columns']) if 'columns' in self.config else ','.join(
            self.schema["properties"].keys())
        params.update({"fields": fields})
        return params

    def post_process(self, row: dict, stream_or_partition_state: dict) -> dict:
        return row


class Posts(FacebookPagesStream):
    name = "posts"
    tap_stream_id = "posts"
    path = "/posts"
    primary_keys = ["id"]
    replication_key = "created_time"
    replication_method = "INCREMENTAL"
    schema_filepath = SCHEMAS_DIR / "posts.json"

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:

        params = super().get_url_params(partition, next_page_token)
        if next_page_token:
            return params

        fields = ','.join(self.config['columns']) if 'columns' in self.config else ','.join(
            self.schema["properties"].keys())
        params.update({"fields": fields})
        return params

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        resp_json = response.json()
        for row in resp_json["data"]:
            row["page_id"] = self.page_id
            yield row


class PostTaggedProfile(FacebookPagesStream):
    name = "post_tagged_profile"
    tap_stream_id = "post_tagged_profile"
    path = "/posts"
    primary_keys = ["id"]
    replication_key = "post_created_time"
    replication_method = "INCREMENTAL"
    schema_filepath = SCHEMAS_DIR / "post_tagged_profile.json"

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:
        params = super().get_url_params(partition, next_page_token)
        if next_page_token:
            return params

        params.update({"fields": "id,created_time,to"})
        return params

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        resp_json = response.json()
        for row in resp_json["data"]:
            parent_info = {
                "page_id": self.page_id,
                "post_id": row["id"],
                "post_created_time": row["created_time"]
            }
            if "to" in row:
                for attachment in row["to"]["data"]:
                    attachment.update(parent_info)
                    yield attachment


class PostAttachments(FacebookPagesStream):
    name = "post_attachments"
    tap_stream_id = "post_attachments"
    path = "/posts"
    primary_keys = ["id"]
    replication_key = "post_created_time"
    replication_method = "INCREMENTAL"
    schema_filepath = SCHEMAS_DIR / "post_attachments.json"

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:
        params = super().get_url_params(partition, next_page_token)
        if next_page_token:
            return params

        params.update({"fields": "id,created_time,attachments"})
        return params

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        resp_json = response.json()
        for row in resp_json["data"]:
            parent_info = {
                "page_id": self.page_id,
                "post_id": row["id"],
                "post_created_time": row["created_time"]
            }
            if "attachments" in row:
                for attachment in row["attachments"]["data"]:
                    if "subattachments" in attachment:
                        for sub_attachment in attachment["subattachments"]["data"]:
                            sub_attachment.update(parent_info)
                            yield sub_attachment
                        attachment.pop("subattachments")
                    attachment.update(parent_info)
                    yield attachment


class PageInsights(FacebookPagesStream):
    name = None
    tap_stream_id = None
    path = "/insights"
    primary_keys = ["id"]
    replication_key = None
    forced_replication_method = "FULL_TABLE"
    schema_filepath = SCHEMAS_DIR / "page_insights.json"

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:
        params = super().get_url_params(partition, next_page_token)
        time = int(t.time())
        day = int(datetime.timedelta(1).total_seconds())
        if not next_page_token:
            until = params['since'] + 8035200
            params.update({"until": until if until <= time else time-day})
        else:
            until = params['until'][0]
            if int(until) > time:
                params['until'][0] = str(time-day)
        params.update({"metric": ",".join(self.metrics)})
        return params

    def get_next_page_token(self, response: requests.Response, previous_token: Optional[Any] = None) -> Any:
        resp_json = response.json()
        if "paging" in resp_json and "next" in resp_json["paging"]:
            time = int(t.time())
            day = int(datetime.timedelta(2).total_seconds())
            params = urllib.parse.parse_qs(urllib.parse.urlparse(resp_json["paging"]["next"]).query)
            since = int(params['since'][0])
            until = int(params['until'][0])
            if since >= time-day or (until >= time and until <= time + day ):
                return None
            return resp_json["paging"]["next"]
        return None

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        resp_json = response.json()
        for row in resp_json["data"]:
            base_item = {
                "name": row["name"],
                "period": row["period"],
                "title": row["title"],
                "id": row["id"],
            }
            if "values" in row:
                for values in row["values"]:
                    if isinstance(values["value"], dict):
                        for key, value in values["value"].items():
                            item = {
                                "context": key,
                                "value": value,
                                "end_time": values["end_time"]
                            }
                            item.update(base_item)
                            yield item
                    else:
                        values.update(base_item)
                        yield values


class PostInsights(FacebookPagesStream):
    name = ""
    tap_stream_id = ""
    # use published_posts instead of feed, as the last one is problematic endpoint
    # path = "/feed"
    path = "/published_posts"
    primary_keys = ["id"]
    replication_key = "post_created_time"
    replication_method = "INCREMENTAL"
    schema_filepath = SCHEMAS_DIR / "post_insights.json"

    def get_url_params(self, partition: Optional[dict], next_page_token: Optional[Any] = None) -> Dict[str, Any]:
        params = super().get_url_params(partition, next_page_token)
        if next_page_token:
            return params

        params.update({"fields": "id,created_time,insights.metric(" + ",".join(self.metrics) + ")"})
        return params

    def parse_response(self, response: requests.Response) -> Iterable[dict]:
        resp_json = response.json()
        for row in resp_json["data"]:
            for insights in row["insights"]["data"]:
                base_item = {
                    "post_id": row["id"],
                    "page_id": self.page_id,
                    "post_created_time": row["created_time"],
                    "name": insights["name"],
                    "period": insights["period"],
                    "title": insights["title"],
                    "description": insights["description"],
                    "id": insights["id"],
                }
                if "values" in insights:
                    for values in insights["values"]:
                        if isinstance(values["value"], dict):
                            for key, value in values["value"].items():
                                item = {
                                    "context": key,
                                    "value": value,
                                }
                                item.update(base_item)
                                yield item
                        else:
                            values.update(base_item)
                            yield values
