from __future__ import print_function
import os
import yaml
import shlex
try:
    basestring
except NameError:
    basestring = str    # Python 3

from .constants import *
from .utils import *

class ConfigError(Exception):
    pass

class ConfigNotFoundError(ConfigError):
    pass

# http://stackoverflow.com/a/9577670
class Loader(yaml.SafeLoader):
    def __init__(self, stream):
        self._root = os.path.split(stream.name)[0]
        super(Loader, self).__init__(stream)

    def from_yaml(self, node):
        '''
        Implementes a !from_yaml constructor with the following syntax:
            !from_yaml filename key

        Arguments:
            filename:   Filename of external YAML document from which to load,
                        relative to the current YAML file.
            key:        Key from external YAML document to return,
                        using a dot-separated syntax for nested keys.

        Examples:
            !from_yaml external.yml pop
            !from_yaml external.yml foo.bar.pop
            !from_yaml "another file.yml" "foo bar.snap crackle.pop"
        '''

        # Load the content from the node, as a scalar
        content = self.construct_scalar(node)

        # Split on unquoted spaces
        try:
            parts = shlex.split(content)
        except UnicodeEncodeError:
            raise yaml.YAMLError('Non-ASCII arguments to !from_yaml are unsupported')

        if len(parts) != 2:
            raise yaml.YAMLError('Two arguments expected to !from_yaml')
        filename, key = parts

        # path is relative to the current YAML document
        path = os.path.join(self._root, filename)

        # Load the other YAML document
        with open(path, 'r') as f:
            doc = yaml.load(f, self.__class__)

        # Retrieve the key
        try:
            cur = doc
            for k in key.split('.'):
                cur = cur[k]
        except KeyError:
            raise yaml.YAMLError('Key "{}" not found in {}'.format(key, filename))
        return cur

Loader.add_constructor('!from_yaml', Loader.from_yaml)


def find_config():
    '''Search up the diretcory hierarchy for .scuba.yml

    Returns: path, rel on success, or None if not found
        path    The absolute path of the directory where .scuba.yml was found
        rel     The relative path from the directory where .scuba.yml was found
                to the current directory
    '''
    cross_fs = 'SCUBA_DISCOVERY_ACROSS_FILESYSTEM' in os.environ
    path = os.getcwd()

    rel = ''
    while True:
        if os.path.exists(os.path.join(path, SCUBA_YML)):
            return path, rel

        if not cross_fs and os.path.ismount(path):
            msg = '{} not found here or any parent up to mount point {}'.format(SCUBA_YML, path) \
                   + '\nStopping at filesystem boundary (SCUBA_DISCOVERY_ACROSS_FILESYSTEM not set).'
            raise ConfigNotFoundError(msg)

        # Traverse up directory hierarchy
        path, rest = os.path.split(path)
        if not rest:
            raise ConfigNotFoundError('{} not found here or any parent directories'.format(SCUBA_YML))

        # Accumulate the relative path back to where we started
        rel = os.path.join(rest, rel)


def _process_script_node(node, name):
    '''Process a script-type node

    This handles nodes that follow the *Common script schema*,
    as outlined in doc/yaml-reference.md.
    '''
    if isinstance(node, basestring):
        # The script is just the text itself
        return [node]


    if isinstance(node, dict):
        # There must be a "script" key, which must be a list of strings
        script = node.get('script')
        if not script:
            raise ConfigError("{}: must have a 'script' subkey".format(name))

        if isinstance(script, list):
            return script

        if isinstance(script, basestring):
            return [script]

        raise ConfigError("{}.script: must be a string or list".format(name))

    raise ConfigError("{}: must be string or dict".format(name))


def _process_environment(node, name):
    # Environment can be either a list of strings ("KEY=VALUE") or a mapping
    # Environment keys and values are always strings
    result = {}

    if not node:
        pass
    elif isinstance(node, dict):
        for k, v in node.items():
            if v is None:
                v = os.getenv(k, '')
            result[k] = str(v)
    elif isinstance(node, list):
        for e in node:
            k, v = parse_env_var(e)
            result[k] = v
    else:
        raise ConfigError("'{}' must be list or mapping, not {}".format(
                name, type(node).__name__))

    return result

def _get_entrypoint(data):
    # N.B. We can't use data.get() here, because that might return
    # None, leading to ambiguity between entrypoint being absent or set
    # to a null value.
    #
    # "Note that a null is different from an empty string and that a
    # mapping entry with some key and a null value is valid and
    # different from not having that key in the mapping."
    #   - http://yaml.org/type/null.html
    key = 'entrypoint'

    if not key in data:
        return None

    ep = data[key]

    # We represent a null value as an empty string.
    if ep is None:
        ep = ''

    if not isinstance(ep, basestring):
        raise ConfigError("'{}' must be a string, not {}".format(
                key, type(ep).__name__))
    return ep


class ScubaAlias(object):
    def __init__(self, name, script, image, entrypoint, environment):
        self.name = name
        self.script = script
        self.image = image
        self.entrypoint = entrypoint
        self.environment = environment

    @classmethod
    def from_dict(cls, name, node):
        script = _process_script_node(node, name)
        image = None
        entrypoint = None
        environment = None

        if isinstance(node, dict):  # Rich alias
            image = node.get('image')
            entrypoint = _get_entrypoint(node)
            environment = _process_environment(
                    node.get('environment'),
                    '{}.{}'.format(name, 'environment'))

        return cls(name, script, image, entrypoint, environment)

class ScubaContext(object):
    pass

class ScubaConfig(object):
    def __init__(self, **data):
        required_nodes = ('image',)
        optional_nodes = ('aliases','hooks','entrypoint','environment')

        # Check for missing required nodes
        missing = [n for n in required_nodes if not n in data]
        if missing:
            raise ConfigError('{}: Required node{} missing: {}'.format(SCUBA_YML,
                    's' if len(missing) > 1 else '', ', '.join(missing)))

        # Check for unrecognized nodes
        extra = [n for n in data if not n in required_nodes + optional_nodes]
        if extra:
            raise ConfigError('{}: Unrecognized node{}: {}'.format(SCUBA_YML,
                    's' if len(extra) > 1 else '', ', '.join(extra)))

        self._image = data['image']
        self._entrypoint = _get_entrypoint(data)
        self._load_aliases(data)
        self._load_hooks(data)
        self._environment = self._load_environment(data)




    def _load_aliases(self, data):
        self._aliases = {}

        for name, node in data.get('aliases', {}).items():
            if ' ' in name:
                raise ConfigError('Alias names cannot contain spaces')
            self._aliases[name] = ScubaAlias.from_dict(name, node)


    def _load_hooks(self, data):
        self._hooks = {}

        for name in ('user', 'root',):
            node = data.get('hooks', {}).get(name)
            if node:
                hook = _process_script_node(node, name)
                self._hooks[name] = hook

    def _load_environment(self, data):
         return _process_environment(data.get('environment'), 'environment')


    @property
    def image(self):
        return self._image

    @property
    def entrypoint(self):
        return self._entrypoint

    @property
    def aliases(self):
        return self._aliases

    @property
    def hooks(self):
        return self._hooks

    @property
    def environment(self):
        return self._environment


    def process_command(self, command):
        '''Processes a user command using aliases

        Arguments:
            command     A user command list (e.g. argv)

        Returns: A ScubaContext object with the following attributes:
            script: a list of command line strings
            image: the docker image name to use
        '''
        result = ScubaContext()
        result.script = None
        result.image = self.image
        result.entrypoint = self.entrypoint
        result.environment = self.environment.copy()

        if command:
            alias = self.aliases.get(command[0])
            if not alias:
                # Command is not an alias; use it as-is.
                result.script = [shell_quote_cmd(command)]
            else:
                # Using an alias
                # Does this alias override the image and/or entrypoint?
                if alias.image:
                    result.image = alias.image
                if alias.entrypoint is not None:
                    result.entrypoint = alias.entrypoint

                # Merge/override the environment
                if alias.environment:
                    result.environment.update(alias.environment)

                if len(alias.script) > 1:
                    # Alias is a multiline script; no additional
                    # arguments are allowed in the scuba invocation.
                    if len(command) > 1:
                        raise ConfigError('Additional arguments not allowed with multi-line aliases')
                    result.script = alias.script

                else:
                    # Alias is a single-line script; perform substituion
                    # and add user arguments.
                    command.pop(0)
                    result.script = [alias.script[0] + ' ' + shell_quote_cmd(command)]

            result.script = flatten_list(result.script)

        return result


def load_config(path):
    try:
        with open(path) as f:
            data = yaml.load(f, Loader)
    except IOError as e:
        raise ConfigError('Error opening {}: {}'.format(SCUBA_YML, e))
    except yaml.YAMLError as e:
        raise ConfigError('Error loading {}: {}'.format(SCUBA_YML, e))

    return ScubaConfig(**(data or {}))
