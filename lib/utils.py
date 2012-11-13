#!/usr/bin/env python
# Copyright 2010 Google Inc.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This file contains various utility classes used by GRR."""



import base64
import posixpath
import re
import socket
import struct
import threading
import time

from google.protobuf import message
from grr.lib import stats
from grr.proto import jobs_pb2


class IPInfo(object):
  UNKNOWN = 0
  INTERNAL = 1
  EXTERNAL = 2
  VPN = 3


def RetrieveIPInfo(ip):
  if not ip:
    return (IPInfo.UNKNOWN, "No ip information.")
  ip = SmartStr(ip)
  if ":" in ip:
    return RetrieveIP6Info(ip)
  return RetrieveIP4Info(ip)

def RetrieveIP4Info(ip):
  """Retrieves information for an IP4 address."""
  if ip.startswith("192"):
    return (IPInfo.INTERNAL, "Internal IP address.")
  try:
    # It's an external IP, let's try to do a reverse lookup.
    res = socket.gethostbyaddr(ip)
    return (IPInfo.EXTERNAL, res[0])
  except (socket.herror, socket.gaierror):
    return (IPInfo.EXTERNAL, "Unknown IP address.")

def RetrieveIP6Info(ip):
  """Retrieves information for an IP6 address."""
  return (IPInfo.INTERNAL, "Internal IP6 address.")


def Proxy(f):
  """A helper to create a proxy method in a class."""

  def Wrapped(self, *args):
    return getattr(self, f)(*args)
  return Wrapped


# This is a synchronize decorator.
def Synchronized(f):
  """Synchronization decorator."""

  def NewFunction(self, *args, **kw):
    with self.lock:
      return f(self, *args, **kw)
  return NewFunction


class InterruptableThread(threading.Thread):
  """A class which exits once the main thread exits."""

  # Class wide constant
  exit = False
  threads = []

  def __init__(self, target=None, args=None, kwargs=None, sleep_time=10, **kw):
    self.target = target
    self.args = args or ()
    self.kwargs = kwargs or {}
    self.sleep_time = sleep_time

    super(InterruptableThread, self).__init__(**kw)
    # Do not hold up program exit
    self.daemon = True

  def Iterate(self):
    """This will be repeatedly called between sleeps."""

  def run(self):
    while not self.exit:
      if self.target:
        self.target(*self.args, **self.kwargs)
      else:
        self.Iterate()

      for _ in range(self.sleep_time):
        if self.exit:
          break
        try:
          if time:
            time.sleep(1)
          else:
            self.exit = True
            break
        except AttributeError:
          # When the main thread exits, time might be already None. We should
          # just ignore that and exit as well.
          self.exit = True
          break


class FastStore(object):
  """This is a cache which expires objects in oldest first manner.

  This implementation first appeared in PyFlag.
  """

  def __init__(self, max_size=10, kill_cb=None):
    """Constructor.

    Args:
       max_size: The maximum number of objects held in cache.
       kill_cb: An optional function which will be called on each
                object terminated from cache.
    """
    self._age = []
    self._hash = {}
    self._limit = max_size
    self._kill_cb = kill_cb
    self.lock = threading.RLock()

  @Synchronized
  def Expire(self):
    """Expires old cache entries."""
    while len(self._age) > self._limit:
      x = self._age.pop(0)
      self.ExpireObject(x)

  @Synchronized
  def Put(self, key, obj):
    """Add the object to the cache."""
    try:
      idx = self._age.index(key)
      self._age.pop(idx)
    except ValueError:
      pass

    self._hash[key] = obj
    self._age.append(key)

    self.Expire()

    return key

  @Synchronized
  def ExpireObject(self, key):
    """Expire a specific object from cache."""
    obj = self._hash.pop(key, None)

    if self._kill_cb and obj is not None:
      self._kill_cb(obj)

    return obj

  @Synchronized
  def ExpireRegEx(self, regex):
    """Expire all the objects with the key matching the regex."""
    for key in self._hash.keys():
      if re.match(regex, key):
        self.ExpireObject(key)

  @Synchronized
  def Get(self, key):
    """Fetch the object from cache.

    Objects may be flushed from cache at any time. Callers must always
    handle the possibility of KeyError raised here.

    Args:
      key: The key used to access the object.

    Returns:
      Cached object.

    Raises:
      KeyError: If the object is not present in the cache.
    """
    # Remove the item and put to the end of the age list
    try:
      idx = self._age.index(key)
      self._age.pop(idx)
      self._age.append(key)
    except ValueError:
      raise KeyError(key)

    return self._hash[key]

  @Synchronized
  def __contains__(self, obj):
    return obj in self._hash

  @Synchronized
  def __getitem__(self, key):
    return self.Get(key)

  @Synchronized
  def Flush(self):
    """Flush all items from cache."""
    while self._age:
      x = self._age.pop(0)
      self.ExpireObject(x)

    self._hash = {}

  @Synchronized
  def __getstate__(self):
    """When pickled the cache is fushed."""
    if self._kill_cb:
      raise RuntimeError("Unable to pickle a store with a kill callback.")

    self.Flush()
    return dict(max_size=self._limit)

  def __setstate__(self, state):
    self.__init__(max_size=state["max_size"])


class TimeBasedCache(FastStore):
  """A Cache which expires based on time."""

  def __init__(self, max_size=10, max_age=600, kill_cb=None):
    """Constructor.

    This cache will refresh the age of the cached object as long as they are
    accessed within the allowed age. The age refers to the time since it was
    last touched.

    Args:
      max_size: The maximum number of objects held in cache.
      max_age: The maximum length of time an object is considered alive.
      kill_cb: An optional function which will be called on each expiration.
    """
    super(TimeBasedCache, self).__init__(max_size, kill_cb)
    self.max_age = max_age

    def HouseKeeper():
      """A housekeeper thread which expunges old objects."""
      if not time:
        # This might happen when the main thread exits, we don't want to raise.
        return

      now = time.time()
      for key in self._age:
        try:
          timestamp, _ = self._hash[key]
          if timestamp + self.max_age < now:
            self.ExpireObject(key)
        except KeyError:
          pass

    # This thread is designed to never finish
    self.house_keeper_thread = InterruptableThread(target=HouseKeeper)
    self.house_keeper_thread.start()

  @Synchronized
  def Get(self, key):
    now = time.time()
    stored = super(TimeBasedCache, self).Get(key)
    if stored[0] + self.max_age < now:
      raise KeyError("Expired")

    # This updates the timestamp in place to keep the object alive
    stored[0] = now

    return stored[1]

  def Put(self, key, obj):
    super(TimeBasedCache, self).Put(key, [time.time(), obj])

  @Synchronized
  def __getstate__(self):
    """When pickled the cache is flushed."""
    if self._kill_cb:
      raise RuntimeError("Unable to pickle a store with a kill callback.")

    self.Flush()
    return dict(max_size=self._limit, max_age=self.max_age)

  def __setstate__(self, state):
    self.__init__(max_size=state["max_size"], max_age=state["max_age"])


class AgeBasedCache(TimeBasedCache):
  """A cache which holds objects for a maximum length of time.

  This differs from the TimeBasedCache which keeps the objects alive as long as
  they are accessed.
  """

  @Synchronized
  def Get(self, key):
    now = time.time()
    stored = FastStore.Get(self, key)
    if stored[0] + self.max_age < now:
      raise KeyError("Expired")

    return stored[1]


class PickleableStore(FastStore):
  """A Cache which can be pickled."""

  @Synchronized
  def __getstate__(self):
    self.lock = None
    return self.__dict__

  def __setstate__(self, state):
    self.__dict__ = state
    self.lock = threading.RLock()


# TODO(user): Eventually slot in Volatility parsing system in here
class Struct(object):
  """A baseclass for parsing binary Structs."""

  # Derived classes must initialize this into an array of (format,
  # name) tuples.
  _fields = None

  def __init__(self, data):
    """Parses ourselves from data."""
    format_str = "".join([x[0] for x in self._fields])
    self.size = struct.calcsize(format_str)

    try:
      parsed_data = struct.unpack(format_str, data[:self.size])

    except struct.error:
      raise RuntimeError("Unable to parse")

    for i in range(len(self._fields)):
      setattr(self, self._fields[i][1], parsed_data[i])

  def __repr__(self):
    """Produce useful text representation of the Struct."""
    dat = []
    for _, name in self._fields:
      dat.append("%s=%s" % (name, getattr(self, name)))
    return "%s(%s)" % (self.__class__.__name__, ", ".join(dat))

  @classmethod
  def GetSize(cls):
    """Calculate the size of the struct."""
    format_str = "".join([x[0] for x in cls._fields])
    return struct.calcsize(format_str)


class DataBlob(object):
  """Wrapper class for DataBlob protobuf."""

  def __init__(self, initializer=None, **kwarg):
    if initializer is None:
      initializer = jobs_pb2.DataBlob(**kwarg)
    self.blob = initializer

  def SetValue(self, value):
    """Receives a value and fills it into a DataBlob."""
    type_mappings = [(unicode, "string"), (str, "data"), (bool, "boolean"),
                     (int, "integer"), (long, "integer"), (dict, "dict")]
    if value is None:
      self.blob.none = "None"
    elif isinstance(value, message.Message):
      # If we have a protobuf save the type and serialized data.
      self.blob.data = value.SerializeToString()
      self.blob.proto_name = value.__class__.__name__
    elif isinstance(value, (list, tuple)):
      self.blob.list.content.extend([DataBlob().SetValue(v) for v in value])
    elif isinstance(value, dict):
      pdict = ProtoDict()
      pdict.FromDict(value)
      self.blob.dict.MergeFrom(pdict.ToProto())
    else:
      for type_mapping, member in type_mappings:
        if isinstance(value, type_mapping):
          setattr(self.blob, member, value)
          return self.blob

      raise RuntimeError("Unsupported type for ProtoDict: %s" % type(value))

    return self.blob

  def GetValue(self):
    """Extracts and returns a single value from a DataBlob."""
    if self.blob.HasField("none"):
      return None
    field_names = ["integer", "string", "data", "boolean", "list", "dict"]
    values = [getattr(self.blob, x) for x in field_names
              if self.blob.HasField(x)]
    if len(values) != 1:
      raise RuntimeError("DataBlob must contain exactly one entry.")
    if self.blob.HasField("proto_name"):
      try:
        pb = getattr(jobs_pb2, self.blob.proto_name)()
        pb.ParseFromString(self.blob.data)
        return pb
      except AttributeError:
        raise RuntimeError("Datablob has unknown protobuf.")
    elif self.blob.HasField("list"):
      return [DataBlob(x).GetValue() for x in self.blob.list.content]
    elif self.blob.HasField("dict"):
      return ProtoDict(values[0]).ToDict()
    else:
      return values[0]


class ProtoDict(object):
  """A high level interface for protobuf Dict objects.

  This effectively converts from a dict to a proto and back.
  The dict may contain strings (python unicode objects), int64,
  or binary blobs (python string objects) as keys and values.
  """

  def __init__(self, initializer=None):
    # Support initializing from a mapping
    self._proto = jobs_pb2.Dict()
    if initializer is not None:
      try:
        for key in initializer:
          new_proto = self._proto.dat.add()
          DataBlob(new_proto.k).SetValue(key)
          DataBlob(new_proto.v).SetValue(initializer[key])
      except (TypeError, AttributeError):
        # String initializer
        if type(initializer) == str:
          self._proto.ParseFromString(initializer)
        else:
          # Support initializing from a protobuf
          self._proto = initializer

  def ToDict(self):
    return dict([(DataBlob(x.k).GetValue(), DataBlob(x.v).GetValue())
                 for x in self._proto.dat])

  def FromDict(self, dictionary):
    for k, v in dictionary.items():
      self[k] = v

  def ToProto(self):
    return self._proto

  def __getitem__(self, key):
    for kv in self._proto.dat:
      if DataBlob(kv.k).GetValue() == key:
        return DataBlob(kv.v).GetValue()

    raise KeyError("%s Not found" % key)

  def Get(self, key, default=None):
    try:
      return self[key]
    except KeyError:
      return default

  get = Proxy("Get")

  def __delitem__(self, key):
    proto = jobs_pb2.Dict()
    for kv in self._proto.dat:
      if DataBlob(kv.k).GetValue() != key:
        proto.dat.add(k=kv.k, v=kv.v)

    self._proto.CopyFrom(proto)

  def __setitem__(self, key, value):
    del self[key]
    new_proto = self._proto.dat.add()
    DataBlob(new_proto.k).SetValue(key)
    DataBlob(new_proto.v).SetValue(value)

  def __str__(self):
    return self._proto.SerializeToString()

  def __iter__(self):
    for kv in self._proto.dat:
      yield DataBlob(kv.k).GetValue()


def GroupBy(items, key):
  """A generator that groups all items by a key.

  Args:
    items:  A list of items or a single item.
    key: A function which given each item will return the key.

  Returns:
    Generator of tuples of (unique keys, list of items) where all items have the
    same key.  session id.
  """
  key_map = {}

  # Make sure we are given a sequence of items here.
  try:
    item_iter = iter(items)
  except TypeError:
    item_iter = [items]

  for item in item_iter:
    key_id = key(item)
    key_map.setdefault(key_id, []).append(item)

  return key_map.iteritems()


def SmartStr(string):
  """Returns a string or encodes a unicode object.

  This function essentially will always return an encoded string. It should be
  used on an interface to the system which must accept a string and not unicode.

  Args:
    string: The string to convert.

  Returns:
    an encoded string.
  """
  if type(string) == unicode:
    return string.encode("utf8", "ignore")

  return str(string)


def SmartUnicode(string):
  """Returns a unicode object.

  This function will always return a unicode object. It should be used to
  guarantee that something is always a unicode object.

  Args:
    string: The string to convert.

  Returns:
    a unicode object.
  """
  if type(string) != unicode:
    try:
      return string.__unicode__()
    except (AttributeError, UnicodeError):
      return str(string).decode("utf8", "ignore")

  return string


def Xor(string, key):
  """Returns a string where each character has been xored with key."""
  return "".join([chr(c ^ key) for c in bytearray(string)])


def XorByteArray(array, key):
  """Xors every item in the array with key and returns it."""
  for i in xrange(len(array)):
    array[i] ^= key
  return array


def FormatAsHexString(num, width=None, prefix="0x"):
  """Takes an int and returns the number formatted as a hex string."""
  # Strip "0x".
  hex_str = hex(num)[2:]
  # Strip "L" for long values.
  hex_str = hex_str.replace("L", "")
  if width:
    hex_str = hex_str.rjust(width, "0")
  return "%s%s" % (prefix, hex_str)


def FormatAsTimestamp(timestamp):
  if not timestamp:
    return "-"
  return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(timestamp))


def NormalizePath(path, sep="/"):
  """A sane implementation of os.path.normpath.

  The standard implementation treats leading / and // as different leading to
  incorrect normal forms.

  NOTE: Its ok to use a relative path here (without leading /) but any /../ will
  still be removed anchoring the path at the top level (e.g. foo/../../../../bar
  => bar).

  Args:
     path: The path to normalize.
     sep: Separator used.

  Returns:
     A normalized path. In this context normalized means that all input paths
     that would result in the system opening the same physical file will produce
     the same normalized path.
  """
  path = SmartUnicode(path)

  path_list = path.split(sep)

  # This is a relative path and the first element is . or ..
  if path_list[0] in [".", "..", ""]:
    path_list.pop(0)

  # Deliberately begin at index 1 to preserve a single leading /
  i = 0

  while True:
    list_len = len(path_list)

    # We begin at the last known good position so we never iterate over path
    # elements which are already examined
    for i in range(i, len(path_list)):
      # Remove /./ form
      if path_list[i] == "." or not path_list[i]:
        path_list.pop(i)
        break

      # Remove /../ form
      elif path_list[i] == "..":
        path_list.pop(i)
        # Anchor at the top level
        if (i == 1 and path_list[0]) or i > 1:
          i -= 1
          path_list.pop(i)
        break

    # If we didnt alter the path so far we can quit
    if len(path_list) == list_len:
      return sep + sep.join(path_list)


def JoinPath(*parts):
  """A sane version of os.path.join.

  The intention here is to append the stem to the path. The standard module
  removes the path if the stem begins with a /.

  Args:
     *parts: parts of the path to join. The first arg is always the root and
        directory traversal is not allowed.

  Returns:
     a normalized path.
  """
  # Ensure all path components are unicode
  parts = [SmartUnicode(path) for path in parts]

  return NormalizePath(u"/".join(parts))


def Join(*parts):
  """Join (AFF4) paths without normalizing.

  A quick join method that can be used to express the precondition that
  the parts are already normalized.

  Args:
    *parts: The parts to join

  Returns:
    The joined path.
  """

  return "/".join(parts)


class Pathspec(object):
  """A client specification for opening files.

  This class implements methods for manipulating the pathspec as a list of
  instructions. We can insert, replace and iterate over all instructions.
  """

  def __init__(self, *args, **kwarg):
    self.elements = []
    self.path_options = None
    for arg in args:
      if isinstance(arg, Pathspec):
        # Make explicit copies of the other pathspec elements.
        for element in arg.elements:
          element_copy = jobs_pb2.Path()
          element_copy.CopyFrom(element)
          self.elements.append(element_copy)

      elif isinstance(arg, jobs_pb2.Path):
        # Break up the protobuf into the elements array.
        proto = jobs_pb2.Path()
        proto.CopyFrom(arg)

        # Unravel the nested proto into a list
        while proto.HasField("nested_path"):
          next_element = proto.nested_path
          proto.ClearField("nested_path")
          self.elements.append(proto)

          proto = next_element

        self.elements.append(proto)

        # Can handle serialized form.
      elif isinstance(arg, str):
        proto = jobs_pb2.Path()
        proto.ParseFromString(arg)
        self.elements.extend(Pathspec(proto).elements)

    # We can also initialize from kwargs.
    if kwarg:
      self.elements.append(jobs_pb2.Path(**kwarg))

  def ToProto(self, output=None):
    if output is None:
      output = jobs_pb2.Path()

    i = output
    for element in self.elements:
      i.MergeFrom(element)
      i = i.nested_path

    return output

  def __len__(self):
    return len(self.elements)

  def __getitem__(self, item):
    return self.elements[item]

  def __iter__(self):
    return iter(self.elements)

  def Replace(self, start_index, end_index, *args, **kwarg):
    """Replace some elements with the new elements.

    Args:
      start_index: The first element to remove.
      end_index: The last element to remove (same as start_index to just remove
           one).
      *arg: New elements.
      **kwarg: Optional constructor for a new pathspec.

    Returns:
      The elements which were removed.
    """
    new_elements = Pathspec(*args, **kwarg)
    result = self.elements[start_index:end_index]
    self.elements = (self.elements[:start_index] + new_elements.elements +
                     self.elements[end_index+1:])

    return result

  def Copy(self):
    """Return a copy of this pathspec."""
    return self.__class__(*self.elements)

  def Insert(self, index, *args, **kwarg):
    new_elements = Pathspec(*args, **kwarg)
    self.elements = (self.elements[:index] + new_elements.elements +
                     self.elements[index:])

  def Append(self, *args, **kwarg):
    new_elements = Pathspec(*args, **kwarg)
    self.elements.extend(new_elements.elements)
    return self

  def CollapsePath(self):
    return JoinPath(*[x.path for x in self.elements])

  def Pop(self, index=0):
    return self.elements.pop(index)

  def __str__(self):
    return "<PathSpec>\n%s</PathSpec>" % str(self.ToProto())

  @property
  def first(self):
    return self.elements[0]

  @property
  def last(self):
    return self.elements[-1]

  def Dirname(self):
    """Get a new copied object with only the directory path."""
    result = self.Copy()

    while result:
      last = result.Pop(-1)
      if NormalizePath(last.path) != "/":
        dirname = NormalizePath(posixpath.dirname(last.path))
        result.Append(path=dirname, pathtype=last.pathtype)
        break

    return result

  def Basename(self):
    for component in reversed(self):
      basename = posixpath.basename(component.path)
      if basename: return basename

    return ""

  def SerializeToString(self):
    return self.ToProto().SerializeToString()


def Grouper(iterable, n):
  """Group iterable into lists of size n. Last list will be short."""
  items = []
  for count, item in enumerate(iterable):
    items.append(item)
    if (count + 1) % n == 0:
      yield items
      items = []
  if items:
    yield items


def EncodeReasonString(reason):
  return base64.urlsafe_b64encode(SmartStr(reason))


def DecodeReasonString(reason):
  return base64.urlsafe_b64decode(SmartStr(reason))


def ToProtoString(string):
  return SmartUnicode(string)
