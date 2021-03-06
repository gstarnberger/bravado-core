# -*- coding: utf-8 -*-
import copy
import functools
import os.path
import warnings
from collections import defaultdict

from jsonschema import RefResolver
from six import iteritems
from six import iterkeys
from six import itervalues
from six.moves.urllib.parse import urljoin
from six.moves.urllib.parse import urlparse
from six.moves.urllib.parse import urlunparse
from six.moves.urllib_parse import ParseResult
from swagger_spec_validator.ref_validators import in_scope

from bravado_core.model import MODEL_MARKER
from bravado_core.schema import is_dict_like
from bravado_core.schema import is_list_like
from bravado_core.schema import is_ref


MARSHAL_REPLACEMENT_PATTERNS = {
    '/': '..',  # / is converted to .. (ie. api_docs/swager.json -> api_docs..swagger.json)
    '#': '|',  # # is converted to | (ie. swager.json#definitions -> swagger.json|definitions)
}


def _marshal_uri(target_uri, origin_uri):
    """
    Translate the URL string representation into a new string which could be used as JSON keys.
    This method is needed because many JSON parsers and reference resolvers are using '/' as
    indicator of object nesting.

    To workaround this limitation we can re-write the url representation in a way that the parsers
    will accept it, for example "#/definitions/data_type" could become "|..definitions..data_type"

    Example: Assume that you have the following JSON document
        {
            "definitions": {
                "a/possible/def": {
                    "type": "object"
                },
                "a": {
                    "possible": {
                        "def": {
                            "type": "string"
                        }
                    }
                },
                "def": {
                    "$ref": "#/definitions/a/possible/def"
                }
            }
        }

    Assuming that the JSON parser is not raising exception the dereferenced value of
    "#/definitions/def" could be {"type": "object"} or {"type": "string"} which is
    an undetermined condition which can lead to weird errors.
    Let's assume instead that the JSON parser will raise an exception in this case
    the JSON object will not be usable.

    To prevent this conditions we are removing possible '/' from the JSON keys.

    :param target_uri: URI to marshal
    :type target_uri: ParseResult
    :param origin_uri: URI of the root swagger spec file
    :type origin_uri: ParseResult

    :return: a string representation of the URL which could be used into the JSON keys
    :rtype: str
    """

    marshalled_target = urlunparse(target_uri)

    if marshalled_target and target_uri.scheme == '':  # scheme is empty for relative paths. It should NOT happen!
        target_uri = ParseResult('file', *target_uri[1:])
        marshalled_target = urlunparse(target_uri)

    if not marshalled_target or target_uri.scheme not in {'file', 'http', 'https'}:
        raise ValueError(
            'Invalid target: \'{target_uri}\''.format(target_uri=urlunparse(target_uri))
        )

    if origin_uri and target_uri.scheme == 'file':
        scheme, netloc, path, params, query, fragment = target_uri

        # Masquerade the absolute file path on the "local" server using
        # relative paths from the root swagger spec file
        spec_dir = os.path.dirname(origin_uri.path)
        scheme = 'lfile'
        path = os.path.relpath(path, spec_dir)
        marshalled_target = urlunparse((scheme, netloc, path, params, query, fragment))

    for src, dst in iteritems(MARSHAL_REPLACEMENT_PATTERNS):
        marshalled_target = marshalled_target.replace(src, dst)
    return marshalled_target


def _warn_if_uri_clash_on_same_marshaled_representation(uri_schema_mappings, marshal_uri):
    """
    Verifies that all the uris present on the definitions are represented by a different marshaled uri.
    If is not the case a warning will filed.

    In case of presence of warning please keep us informed about the issue, in the meantime you can
    workaround this calling directly ``flattened_spec(spec, marshal_uri_function)`` passing your
    marshalling function.
    """
    # Check that URIs are NOT clashing to same marshaled representation
    marshaled_uri_mapping = defaultdict(set)
    for uri in iterkeys(uri_schema_mappings):
        marshaled_uri_mapping[marshal_uri(uri)].add(uri)

    if len(marshaled_uri_mapping) != len(uri_schema_mappings):
        # At least two uris clashed to the same marshaled representation
        for marshaled_uri, uris in iteritems(marshaled_uri_mapping):
            if len(uris) > 1:
                warnings.warn(
                    message='{s_uris} clashed to {marshaled}'.format(
                        s_uris=', '.join(sorted(urlunparse(uri) for uri in uris)),
                        marshaled=marshaled_uri,
                    ),
                    category=Warning,
                )


_TYPE_SCHEMA, _TYPE_PATH_ITEM, _TYPE_PARAMETER, _TYPE_RESPONSE = range(4)
_TYPE_PROPERTY_HOLDER_MAPPING = {
    _TYPE_PARAMETER: 'parameters',
    _TYPE_RESPONSE: 'responses',
    _TYPE_SCHEMA: 'definitions',
}


def _determine_object_type(object_dict):
    """
    Use best guess to determine the object type based on the object keys.

    NOTE: it assumes that the base swagger specs are validated and perform type detection for
    the four types of object that could be references in the specs: parameter, path item, response and schema.

    :return: determined type of ``object_dict``. The return values are:
        - ``_TYPE_SCHEMA`` for schema objects
        - ``_TYPE_PATH_ITEM`` for path item objects
        - ``_TYPE_PARAMETER`` for parameter objects
        - ``_TYPE_RESPONSE`` for parameter response objects

    :rtype: int
    """
    if 'in' in object_dict and 'name' in object_dict:
        # A parameter object is the only object type that could contain 'in' and 'name' at the same time
        return _TYPE_PARAMETER
    else:
        http_operations = {'get', 'put', 'post', 'delete', 'options', 'head', 'patch'}
        # A path item object MUST have defined at least one http operation and could optionally have 'parameter'
        # attribute. NOTE: patterned fields (``^x-``) are acceptable in path item objects
        object_keys = {key for key in iterkeys(object_dict) if not key.startswith('x-')}
        if object_keys.intersection(http_operations):
            remaining_keys = object_keys.difference(http_operations)
            if not remaining_keys or remaining_keys == {'parameters'}:
                return _TYPE_PATH_ITEM
        else:
            # A response object has:
            #  - mandatory description field
            #  - optional schema, headers and examples field
            #  - no other fields are allowed
            response_allowed_keys = {'description', 'schema', 'headers', 'examples'}

            # If description field is specified and there are no other fields other the allowed response fields
            if 'description' in object_keys and not object_keys - response_allowed_keys:
                return _TYPE_RESPONSE
            else:
                # A schema object has:
                #  - no mandatory parameters
                #  - long list of optional parameters (ie. description, type, items, properties, discriminator, etc.)
                #  - no other fields are allowed
                # NOTE: In case the method is mis-determining the type of a schema object, confusing it with a
                #       response type it will be enough to add, to the object, one key that is not defined
                #       in ``response_allowed_keys``.  (ie. ``additionalProperties: {}``, implicitly defined be specs)
                return _TYPE_SCHEMA


def flattened_spec(
    spec_dict, spec_resolver=None, spec_url=None, http_handlers=None,
    marshal_uri_function=_marshal_uri, spec_definitions=None,
):
    """
    Flatten Swagger Specs description into an unique and JSON serializable document.
    The flattening injects in place the referenced [path item objects](https://swagger.io/specification/#pathItemObject)
    while it injects in '#/parameters' the [parameter objects](https://swagger.io/specification/#parameterObject),
    in '#/definitions' the [schema objects](https://swagger.io/specification/#schemaObject) and in
    '#/responses' the [response objects](https://swagger.io/specification/#responseObject).

    Note: the object names in '#/definitions', '#/parameters' and '#/responses' are evaluated by
    ``marshal_uri_function``, the default method takes care of creating unique names for all the used references.
    Since name clashing are still possible take care that a warning could be filed.
    If it happen please report to us the specific warning text and the specs that generated it.
    We can work to improve it and in the mean time you can "plug" a custom marshalling function.

    Note: https://swagger.io/specification/ has been update to track the latest version of the Swagger/OpenAPI specs.
    Please refer to https://github.com/OAI/OpenAPI-Specification/blob/3.0.0/versions/2.0.md#responseObject for the
    most recent Swagger 2.0 specifications.

    :param spec_dict: Swagger Spec dictionary representation. Note: the method assumes that the specs are valid specs.
    :type spec_dict: dict
    :param spec_resolver: Swagger Spec resolver for fetching external references
    :type spec_resolver: RefResolver
    :param spec_url: Base url of your Swagger Specs. It is used to hide internal paths during uri marshaling.
    :type spec_url: str
    :param http_handlers: custom handlers for retrieving external specs.
        The expected format is {protocol: read_protocol}, with read_protocol similar to  read_protocol=lambda uri: ...
        An example could be provided by ``bravado_core.spec.build_http_handlers``
    :type http_handlers: dict
    :param marshal_uri_function: function used to marshal uris in string suitable to be keys in Swagger Specs.
    :type marshal_uri_function: Callable with the same signature of ``_marshal_uri``
    :param spec_definitions: known swagger definitions (hint: definitions attribute of bravado_core.spec.Spec instance)
    :type dict: bravado_core.spec.Spec

    :return: Flattened representation of the Swagger Specs
    :rtype: dict
    """

    # Create internal copy of spec_dict to avoid external dict pollution
    spec_dict = copy.deepcopy(spec_dict)

    if spec_url is None:
        warnings.warn(
            message='It is recommended to set origin_url to your spec before flattering it. '
                    'Doing so internal paths will be hidden, reducing the amount of exposed information.',
            category=Warning,
        )

    if not spec_resolver:
        if not spec_url:
            raise ValueError('spec_resolver or spec_url should be defined')

        spec_resolver = RefResolver(
            base_uri=spec_url,
            referrer=spec_dict,
            handlers=http_handlers or {},
        )

    if spec_definitions is None:
        warnings.warn(
            message='Un-referenced models cannot be un-flattened if spec_definitions is not present',
            category=Warning,
        )

    known_mappings = {
        key: {}
        for key in itervalues(_TYPE_PROPERTY_HOLDER_MAPPING)
    }

    # Define marshal_uri method to be used by descend
    marshal_uri = functools.partial(
        marshal_uri_function,
        origin_uri=urlparse(spec_url) if spec_url else None,
    )

    # Avoid object attribute extraction during descend
    resolve = spec_resolver.resolve

    def descend(value):
        if is_ref(value):
            uri, deref_value = resolve(value['$ref'])

            # Update spec_resolver scope to be able to dereference relative specs from a not root file
            with in_scope(spec_resolver, {'x-scope': [uri]}):
                object_type = _determine_object_type(object_dict=deref_value)
                if object_type is _TYPE_PATH_ITEM:
                    return descend(value=deref_value)
                else:
                    mapping_key = _TYPE_PROPERTY_HOLDER_MAPPING.get(object_type, 'definitions')

                    uri = urlparse(uri)
                    if uri not in known_mappings.get(mapping_key, {}):
                        # The placeholder is present to interrupt the recursion
                        # during the recursive traverse of the data model (``descend``)
                        known_mappings[mapping_key][uri] = None

                        known_mappings[mapping_key][uri] = descend(value=deref_value)

                    return {'$ref': '#/{}/{}'.format(mapping_key, marshal_uri(uri))}

        elif is_dict_like(value):
            return {
                key: descend(value=subval)
                for key, subval in iteritems(value)
            }

        elif is_list_like(value):
            return [
                descend(value=subval)
                for index, subval in enumerate(value)
            ]

        else:
            return value

    resolved_spec = descend(value=spec_dict)

    if spec_definitions is not None:
        from bravado_core.spec import Spec  # local import due to circular dependency
        # Creating the bravado_core.spec.Spec object will trigger models discovery and tagging.
        # The process will add x-model key to ``known_mappings['definitions']`` items
        Spec.from_dict(
            # Minimalistic swagger spec like object
            # it's not a valid spec due to lack of info and paths, but it's good enough to trigger model discovery
            spec_dict={
                'definitions': {
                    marshal_uri(uri): value
                    for uri, value in iteritems(known_mappings['definitions'])
                }
            },
            config={'validate_swagger_spec': False},  # Not validate specs, which are known to not be valid
        )

        flatten_models = {
            # schema objects might not have a "type" set so they won't be tagged as models
            definition.get(MODEL_MARKER)
            for definition in itervalues(known_mappings['definitions'])
        }

        for model_name, model_type in iteritems(spec_definitions):
            if model_name in flatten_models:
                continue
            model_url = urlparse(urljoin(spec_url, '#/definitions/{}'.format(model_name)))
            known_mappings['definitions'][model_url] = descend(value=model_type._model_spec)

    for mapping_key, mappings in iteritems(known_mappings):
        _warn_if_uri_clash_on_same_marshaled_representation(
            uri_schema_mappings=mappings,
            marshal_uri=marshal_uri,
        )
        if len(mappings) > 0:
            resolved_spec.update({mapping_key: {
                marshal_uri(uri): value
                for uri, value in iteritems(mappings)
            }})

    return resolved_spec
