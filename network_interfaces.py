"""Discover local addresses with stable Windows and Linux adapter identities."""
from ipaddress import ip_address
import os
import socket
import sys


def interface_addresses():
    if os.name == 'nt':
        rows = _windows_addresses()
    elif sys.platform.startswith('linux') and os.environ.get('LINGXI_ANDROID') != '1':
        rows = _linux_addresses()
    else:
        rows = [{'address':record[4][0].split('%',1)[0], 'interface_id':'', 'interface_name':''}
                for record in socket.getaddrinfo(socket.gethostname(),None,type=socket.SOCK_STREAM)]
    unique = {}
    for row in rows:
        address = ip_address(row['address'])
        if (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast
                or address.version == 6 and address.ipv4_mapped is not None):
            continue
        row['address'] = str(address)
        row['family'] = 'ipv4' if address.version == 4 else 'ipv6'
        unique[row['address']] = row
    return sorted(unique.values(),key=lambda row:(ip_address(row['address']).version,int(ip_address(row['address']))))


def _linux_addresses():
    import ctypes as c

    class Sockaddr(c.Structure):
        _fields_ = [('family', c.c_ushort), ('data', c.c_ubyte * 14)]

    class Interface(c.Structure):
        pass
    Interface._fields_ = [('next', c.POINTER(Interface)), ('name', c.c_char_p), ('flags', c.c_uint),
        ('address', c.POINTER(Sockaddr)), ('netmask', c.POINTER(Sockaddr)),
        ('destination', c.POINTER(Sockaddr)), ('data', c.c_void_p)]

    library = c.CDLL(None, use_errno=True)
    library.getifaddrs.argtypes = [c.POINTER(c.POINTER(Interface))]
    library.getifaddrs.restype = c.c_int
    library.freeifaddrs.argtypes = [c.POINTER(Interface)]
    library.freeifaddrs.restype = None
    first = c.POINTER(Interface)()
    if library.getifaddrs(c.byref(first)):
        error = c.get_errno()
        raise OSError(error, os.strerror(error))
    rows, interface = [], first
    try:
        while interface:
            item = interface.contents
            if item.flags & 1 and item.address:  # IFF_UP; ignore AF_PACKET entries.
                family = item.address.contents.family
                if family in (socket.AF_INET, socket.AF_INET6):
                    offset, length = (4, 4) if family == socket.AF_INET else (8, 16)
                    raw = c.string_at(item.address, offset + length)
                    name = os.fsdecode(item.name)
                    rows.append({'address': socket.inet_ntop(family, raw[offset:offset + length]),
                                 'interface_id': 'linux:' + name, 'interface_name': name})
            interface = item.next
    finally:
        library.freeifaddrs(first)
    return rows


def _windows_addresses():
    import ctypes as c
    from ctypes import wintypes as w

    class SocketAddress(c.Structure):
        _fields_ = [('pointer',c.c_void_p),('length',c.c_int)]

    class Unicast(c.Structure):
        pass
    Unicast._fields_ = [('alignment',c.c_ulonglong),('next',c.POINTER(Unicast)),('address',SocketAddress)]

    class Adapter(c.Structure):
        pass
    Adapter._fields_ = [('alignment',c.c_ulonglong),('next',c.POINTER(Adapter)),('name',c.c_char_p),
        ('unicast',c.POINTER(Unicast)),('anycast',c.c_void_p),('multicast',c.c_void_p),('dns',c.c_void_p),
        ('suffix',c.c_wchar_p),('description',c.c_wchar_p),('friendly',c.c_wchar_p),
        ('physical',c.c_ubyte*8),('physical_length',w.ULONG),('flags',w.ULONG),('mtu',w.ULONG),
        ('kind',w.ULONG),('status',c.c_int)]
    function = c.WinDLL('iphlpapi').GetAdaptersAddresses
    function.argtypes = [w.ULONG,w.ULONG,c.c_void_p,c.POINTER(Adapter),c.POINTER(w.ULONG)]
    function.restype = w.ULONG
    size = w.ULONG()
    result = function(socket.AF_UNSPEC,14,None,None,c.byref(size))
    if result == 232:
        return []
    if result != 111:
        raise OSError(result,'无法读取网卡地址')
    buffer = c.create_string_buffer(size.value)
    first = c.cast(buffer,c.POINTER(Adapter))
    result = function(socket.AF_UNSPEC,14,None,first,c.byref(size))
    if result:
        raise OSError(result,'无法读取网卡地址')
    rows, adapter = [], first
    while adapter:
        value = adapter.contents
        # RAS creates a new adapter GUID on redial; the connection profile name
        # remains stable. Physical/virtual NICs retain their adapter GUID.
        identity = 'ppp:'+value.friendly if value.kind == 23 else value.name.decode('ascii')
        address = value.unicast if value.status == 1 else None
        while address:
            item = address.contents.address
            raw = c.string_at(item.pointer,item.length)
            family = int.from_bytes(raw[:2],'little')
            if family in (socket.AF_INET,socket.AF_INET6):
                offset,length = (4,4) if family == socket.AF_INET else (8,16)
                rows.append({'address':socket.inet_ntop(family,raw[offset:offset+length]),
                    'interface_id':identity,'interface_name':value.friendly})
            address = address.contents.next
        adapter = value.next
    return rows
