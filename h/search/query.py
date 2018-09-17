# -*- coding: utf-8 -*-

from __future__ import unicode_literals
from dateutil.parser import parse
from dateutil import tz
from datetime import datetime as dt
from h._compat import urlparse

from h import storage
from h.util import uri
from elasticsearch_dsl import Q
from elasticsearch_dsl.query import SimpleQueryString

LIMIT_DEFAULT = 20
# Elasticsearch requires offset + limit must be <= 10,000.
LIMIT_MAX = 200
OFFSET_MAX = 9800


def wildcard_uri_is_valid(wildcard_uri):
    """
    Return True if uri contains wildcards in appropriate places, return False otherwise.

    Wildcards are not permitted in the scheme or netloc of the uri.
    """
    if "*" not in wildcard_uri and "?" not in wildcard_uri:
        return False
    try:
        normalized_uri = urlparse.urlparse(wildcard_uri)

        # Remove all parts of the url except the scheme, netloc, and provide a substitute
        # path value "p" so that uri's that only have a scheme and path are still valid.
        uri_parts = (normalized_uri.scheme, normalized_uri.netloc, "p", "", "", "")

        # Remove the "p" standing for path from the end of the uri.
        begining_of_uri = urlparse.urlunparse(uri_parts)[:-1]

        # If a wildcard was in the scheme the uri may come back as "" (a falsey value).
        if begining_of_uri and wildcard_uri.startswith(begining_of_uri):
            return True
    except ValueError:
        pass
    return False


def _pop_param_values(params, key):
    """ Pops and returns all values of the key in params"""
    values = params.getall(key)
    if values:
        del params[key]
    return values


class KeyValueMatcher(object):
    """
    Adds any parameters as straightforward key-value matchers.

    This is intended to be run after all other matchers so that any
    remaining params not popped by any other qualifier get dealt with here.
    """

    def __call__(self, search, params):
        for key, value in params.items():
            search = search.filter("match", **{key: value})
        return search


class Limiter(object):
    """
    Limits the number of annotations returned by the search.

    Searchers for annotations starting at offset and ending at limit.
    """

    def __call__(self, search, params):
        starting_offset = self._extract_offset(params)
        ending_offset = starting_offset + self._extract_limit(params)
        return search[starting_offset:ending_offset]

    def _extract_offset(self, params):
        offset = params.pop("offset", 0)
        try:
            val = int(offset)
            # val must be 0 <= val <= OFFSET_MAX.
            val = min(val, OFFSET_MAX)
            val = max(val, 0)
        except ValueError:
            return 0
        return val

    def _extract_limit(self, params):
        limit = params.pop("limit", LIMIT_DEFAULT)
        try:
            val = int(limit)
            # val must be 0 <= val <= LIMIT_MAX but if
            # val < 0 then set it to the default.
            val = min(val, LIMIT_MAX)
            if val < 0:
                return LIMIT_DEFAULT
        except ValueError:
            return LIMIT_DEFAULT
        return val


class Sorter(object):
    """
    Sorts and returns annotations after search_after.

    Sorts annotations by sort (the key to sort by)
    and the order (the order in which to sort by).

    Returns annotations after search_after. search_after
    must be the value of the annotation's sort field.
    """

    def __call__(self, search, params):
        sort_by = params.pop("sort", "updated")
        # Sorting must be done on non-analyzed fields.
        if sort_by == "user":
            sort_by = "user_raw"

        # Since search_after depends on the field that the annotations are
        # being sorted by, it is set here rather than in a separate class.
        search_after = params.pop("search_after", None)
        if search_after:
            if sort_by in ["updated", "created"]:
                search_after = self._parse_date(search_after)

        if search_after:
            search = search.extra(search_after=[search_after])

        return search.sort(
            {sort_by:
                {"order": params.pop("order", "desc"),

                 # `unmapped_type` causes unknown fields specified as arguments to
                 # `sort` behave as if all documents contained empty values of the
                 # given type. Without this, specifying eg. `sort=foobar` throws
                 # an exception.
                 #
                 # We use the field type `boolean` to assist with migration because
                 # that exists in both ES 1 and ES 6.
                 "unmapped_type": "boolean"}}
        )

    def _parse_date(self, str_value):
        """
        Converts a string to a float representing miliseconds since the epoch.

        Since the elasticsearch date parser is not run on search_after,
        the date must be converted to ms since the epoch as that is how
        the dates are stored in the elasticsearch index.
        """
        # Dates like "2017" can also be cast as floats so if a number is less
        # than 9999 it is assumed to be a year and not ms since the epoch.
        try:
            date = float(str_value)
            if date < 9999:
                raise ValueError("This is not in the form ms since the epoch.")
            return date
        except ValueError:
            try:
                date = parse(str_value)
                # If timezone isn't specified assume it's utc.
                if not date.tzinfo:
                    date = date.replace(tzinfo=tz.tzutc())
                epoch = dt.utcfromtimestamp(0).replace(tzinfo=tz.tzutc())
                return (date - epoch).total_seconds() * 1000.0
            except ValueError:
                pass


class TopLevelAnnotationsFilter(object):

    """Matches top-level annotations only, filters out replies."""

    def __call__(self, search, _):
        return search.exclude("exists", field="references")


class AuthorityFilter(object):

    """
    Match only annotations created by users belonging to a specific authority.
    """

    def __init__(self, authority):
        self.authority = authority

    def __call__(self, search, params):
        return search.filter("term", authority=self.authority)


class AuthFilter(object):

    """
    A filter that selects only annotations the user is authorised to see.

    Only annotations that are shared, or private annotations made
    by the logged-in user will pass through this filter.
    """

    def __init__(self, request):
        """
        Initialize a new AuthFilter.

        :param request: the pyramid.request object
        """
        self.request = request

    def __call__(self, search, params):
        userid = self.request.authenticated_userid
        if userid is None:
            return search.filter("term", shared=True)

        return search.filter(Q("bool",
                               should=[Q("term", shared=True),
                                       Q("term", user_raw=userid)]))


class GroupFilter(object):

    """
    Matches only those annotations belonging to the specified group.
    """

    def __call__(self, search, params):
        # Remove parameter if passed, preventing fall-through to default query
        group = params.pop("group", None)

        if group is not None:
            return search.filter("term", group=group)
        return search


class GroupAuthFilter(object):
    """Filter out groups that the request isn't authorized to read."""

    def __init__(self, request):
        self.user = request.user
        self.group_service = request.find_service(name="group")

    def __call__(self, search, _):
        groups = self.group_service.groupids_readable_by(self.user)
        return search.filter("terms", group=groups)


class UriFilter(object):

    """
    A filter that selects only annotations where the 'uri' parameter matches.
    """

    def __init__(self, request):
        """Initialize a new UriFilter.

        :param request: the pyramid.request object

        """
        self.request = request

    def __call__(self, search, params):
        if 'uri' not in params and 'url' not in params:
            return search
        query_uris = [v for k, v in params.items() if k in ['uri', 'url']]
        if 'uri' in params:
            del params['uri']
        if 'url' in params:
            del params['url']

        uris = set()
        for query_uri in query_uris:
            expanded = storage.expand_uri(self.request.db, query_uri)

            us = [uri.normalize(u) for u in expanded]
            uris.update(us)
        return search.filter(
            'terms', **{'target.scope': list(uris)})


class UriCombinedWildcardFilter(object):

    """
    A filter that selects only annotations where the uri matches.

    If separate_keys is True:
        Wildcard searches are only performed on wildcard_uri's, and exact match searches
        are performed on uri/url parameters.
    If separate_keys is False:
        uri/url parameters are expected to contain exact matches and wildcard matches.
        Values containing wildcards are interpreted as wildcard searches.

    If specifying a uri with wildcards:
    * will match any character sequence (including an empty one), and a ? will match
    any single character.
    """

    def __init__(self, request, separate_keys=False):
        """Initialize a new UriFilter.

        :param request: the pyramid.request object
        :param separate_keys: if True will treat wildcard_uri as wildcards and uri/url
            as exact match. If False will treat any values in uri/url containing wildcards
            ("?" or "*") as wildcard searches.

        """
        self.request = request
        self.separate_keys = separate_keys

    def __call__(self, search, params):
        # If there are no uris to search for there is nothing to do so return early.
        if not ("uri" in params or "url" in params or "wildcard_uri" in params):
            return search

        if self.separate_keys:
            uris = _pop_param_values(params, 'uri') + _pop_param_values(params, 'url')
            wildcard_uris = _pop_param_values(params, 'wildcard_uri')
        else:
            uris = _pop_param_values(params, 'uri') + _pop_param_values(params, 'url')
            # Split into wildcard uris and non wildcard uris.
            wildcard_uris = [u for u in uris if "*" in u or "?" in u]
            uris = [u for u in uris if "*" not in u and "?" not in u]

        # Only add valid uri's to the search list.
        wildcard_uris = self._normalize_uris(
            [u for u in wildcard_uris if wildcard_uri_is_valid(u)],
            normalize_method=self._wildcard_uri_normalized)
        uris = self._normalize_uris(uris)

        queries = []
        if wildcard_uris:
            queries = [Q('wildcard', **{"target.scope": u}) for u in wildcard_uris]
        if uris:
            queries.append(Q("terms", **{'target.scope': uris}))
        return search.query('bool', should=queries)

    def _normalize_uris(self, query_uris, normalize_method=uri.normalize):
        uris = set()
        for query_uri in query_uris:
            expanded = storage.expand_uri(self.request.db, query_uri)

            us = [normalize_method(u) for u in expanded]
            uris.update(us)
        return list(uris)

    def _wildcard_uri_normalized(self, wildcard_uri):
        """
        Same as uri.normalized but it doesn't strip ending `?` from uri's.

        It's possible to have a wildcard at the end of a uri, however
        uri.normalize strips `?`s from the end of uris and something like
        http://foo.com/* will not be normalized to http://foo.com* without
        removing the `*` before normalization. To compensate for this,
        we check for an ending wildcard and add it back after normalization.

        While it's possible to escape `?` and `*` using \\, the uri.normalize
        converts \\ to encoded url format which does not behave the same in
        elasticsearch. Thus, escaping wildcard characters is not currently
        supported.
        """
        trailing_wildcard = ""
        if wildcard_uri.endswith("?") or wildcard_uri.endswith("*"):
            trailing_wildcard = wildcard_uri[-1]
            wildcard_uri = wildcard_uri[:-1]
        wildcard_uri = uri.normalize(wildcard_uri)
        wildcard_uri += trailing_wildcard
        return wildcard_uri


class UserFilter(object):

    """
    A filter that selects only annotations where the 'user' parameter matches.
    """

    def __call__(self, search, params):
        if 'user' not in params:
            return search

        users = [v.lower() for k, v in params.items() if k == 'user']
        del params['user']

        return search.filter("terms", user=users)


class DeletedFilter(object):

    """
    A filter that only returns non-deleted documents.

    Documents are not getting deleted from the index, they only get marked as
    deleted.
    """

    def __call__(self, search, _):
        return search.exclude("exists", field="deleted")


class NipsaFilter(object):
    """Return an Elasticsearch filter for filtering out NIPSA'd annotations."""

    def __init__(self, request):
        self.group_service = request.find_service(name='group')
        self.user = request.user

    def __call__(self, search, _):
        """Filter out all NIPSA'd annotations except the current user's."""
        # If any one of these "should" clauses is true then the annotation will
        # get through the filter.
        should_clauses = [Q("bool", must_not=Q("term", nipsa=True)),
                          Q("exists", field="thread_ids")]

        if self.user is not None:
            # Always show the logged-in user's annotations even if they have nipsa.
            should_clauses.append(Q("term", user=self.user.userid.lower()))

            # Also include nipsa'd annotations for groups that the user created.
            created_groups = self.group_service.groupids_created_by(self.user)
            if created_groups:
                should_clauses.append(Q("terms", group=created_groups))

        return search.filter(Q("bool", should=should_clauses))


class AnyMatcher(object):

    """
    Matches the contents of a selection of fields against the `any` parameter.
    """

    def __call__(self, search, params):
        if "any" not in params:
            return search
        qs = ' '.join([v for k, v in params.items() if k == "any"])
        del params["any"]
        return search.query(
            SimpleQueryString(
                query=qs,
                fields=["quote", "tags", "text", "uri.parts"],
                # default_operator='or',
            )
        )


class TagsMatcher(object):

    """Matches the tags field against 'tag' or 'tags' parameters."""

    def __call__(self, search, params):
        tags = set(v for k, v in params.items() if k in ['tag', 'tags'])
        try:
            del params['tag']
            del params['tags']
        except KeyError:
            pass
        matchers = [Q("match", tags={"query": t, "operator": "and"})
                    for t in tags]
        if matchers:
            return search.query(
                Q('bool', must=matchers)
            )
        return search


class RepliesMatcher(object):

    """Matches any replies to any of the given annotation ids."""

    def __init__(self, ids):
        self.annotation_ids = ids

    def __call__(self, search, _):
        return search.query(
            Q('bool', must=[Q('terms', references=self.annotation_ids)])
        )


class TagsAggregation(object):
    def __init__(self, limit=10):
        self.limit = limit
        self.name = "tags"

    def __call__(self, search, _):
        search.aggs.bucket(self.name, 'terms', size=self.limit, field='tags_raw')

    def parse_result(self, result):
        return [
            {'tag': b["key"], 'count': b["doc_count"]}
            for b in result[self.name]["buckets"]
        ]


class UsersAggregation(object):
    def __init__(self, limit=10):
        self.limit = limit
        self.name = "users"

    def __call__(self, search, _):
        search.aggs.bucket(self.name, 'terms', size=self.limit, field='user_raw')

    def parse_result(self, result):
        return [
            {'user': b["key"], 'count': b["doc_count"]}
            for b in result[self.name]["buckets"]
        ]
