#
# Copyright (c) 2019 Yutaro Hayakawa
# Licensed under the Apache License, Version 2.0 (the "License")
#
import re
import yaml
import click
import socket
import argparse
import textwrap
import ipaddress
import subprocess
import dataclasses
from bcc import BPF
from ctypes import *


# Ethernet type name <=> Ethernet type mapping
L3_PROTO_TO_ID = {}
ID_TO_L3_PROTO = {}
def init_ethertypes_mapping():
    for line in open("/etc/ethertypes"):
        spl = line.split()
        if len(spl) == 0 or spl[0] == "#":
            continue
        ident = str(socket.htons(int(spl[1], 16)))
        L3_PROTO_TO_ID[spl[0]] = ident
        ID_TO_L3_PROTO[ident] = spl[0]


# Protocol name <=> Protocol number mapping
L4_PROTO_TO_ID = {}
ID_TO_L4_PROTO = {}
def init_protocol_mapping():
    for line in open("/etc/protocols"):
        spl = line.split()
        if len(spl) == 0 or spl[0] == "#":
            continue
        L4_PROTO_TO_ID[spl[2]] = spl[1]
        ID_TO_L4_PROTO[spl[1]] = spl[2]


class V4Addrs(Structure):
    _fields_ = [("saddr", c_uint32), ("daddr", c_uint32)]


class V6Addrs(Structure):
    _fields_ = [("saddr", c_uint8 * 16), ("daddr", c_uint8 * 16)]


class IPAddrs(Union):
    _fields_ = [("v4", V4Addrs), ("v6", V6Addrs)]


class EventData(Structure):
    _anonymous = "addrs"
    _fields_ = [
        ("event_id", c_uint8),
        ("l4_protocol", c_uint8),
        ("l3_protocol", c_uint16),
        ("addrs", IPAddrs),
        ("sport", c_uint16),
        ("dport", c_uint16),
    ]


@dataclasses.dataclass(eq=True, frozen=True)
class Flow:
    l3_protocol: str
    l4_protocol: str
    saddr: str
    daddr: str
    sport: int
    dport: int


class IPFTracer:
    def __init__(self, **kwargs):
        self.args = kwargs
        self.functions = None
        self.id_to_ename = []
        self.read_functions()
        self.probes = self.build_probes()

    def resolve_event_name(self, eid):
        return self.id_to_ename[eid]

    def read_functions(self):
        base_fname = f"conf/base/{self.args['kernel_version']}.yaml"
        with open(base_fname) as bf:
            self.functions = yaml.load(bf, Loader=yaml.FullLoader)["functions"]

    def build_l3_protocol_opt(self, protocol):
        if protocol == "any":
            return ["-D", "L3_PROTOCOL_ANY"]
        else:
            return ["-D", "L3_PROTOCOL=" + L3_PROTO_TO_ID[protocol]]


    def build_l4_protocol_opt(self, protocol):
        if protocol == "any":
            return ["-D", "L4_PROTOCOL_ANY"]
        else:
            return ["-D", "L4_PROTOCOL=" + L4_PROTO_TO_ID[protocol]]


    def inet_addr4(self, addr):
        a = ipaddress.IPv4Address(addr).packed
        return str(int.from_bytes(a, byteorder="little"))


    def build_addr4_opt(self, addr, direction):
        if addr == "any":
            return ["-D", direction + "ADDRV4_ANY"]
        else:
            return ["-D", direction + "ADDRV4=" + self.inet_addr4(addr)]


    def inet_addr6(self, addr):
        p = ipaddress.IPv6Address(addr).packed
        a = ",".join(list(map(lambda b: str(b), p)))
        return a


    def build_addr6_opt(self, addr, direction):
        if addr == "any":
            return ["-D", direction + "ADDRV6_ANY"]
        else:
            return ["-D", direction + "ADDRV6=" + self.inet_addr6(addr)]


    def build_port_opt(self, port, direction):
        if port == "any":
            return ["-D", direction + "PORT_ANY"]
        else:
            return ["-D", direction + "PORT=" + port]

    def build_opts(self):
        opts = []
        opts += self.build_l3_protocol_opt(self.args["l3proto"])
        opts += self.build_l4_protocol_opt(self.args["l4proto"])
        opts += self.build_addr4_opt(self.args["saddr4"], "S")
        opts += self.build_addr4_opt(self.args["daddr4"], "D")
        opts += self.build_addr6_opt(self.args["saddr6"], "S")
        opts += self.build_addr6_opt(self.args["daddr6"], "D")
        opts += self.build_port_opt(self.args["sport"], "S")
        opts += self.build_port_opt(self.args["dport"], "D")
        return opts

    def build_probes(self):
        eid = 0
        ret = open("ipftrace.bpf.c").read()
        for group, events in self.functions.items():
            for e in events:
                self.id_to_ename.append(e["name"])
                probe = textwrap.dedent(
                    f"""
                    void kprobe__{e['name']}({ ', '.join(['struct pt_regs *ctx'] + e['args']) }) {{
                      struct event_data e = {{ {eid} }};
                      if (!match(ctx, skb, &e)) {{
                        return;
                      }}
                      action(ctx, &e);
                    }}
                    """
                )
                ret += probe
                eid += 1
        return ret

    def list_functions(self):
        print("Available groups and functions")
        for g, l in self.functions.items():
            print(g)
            for e in l:
                print(f"  {e['name']}")


def guess_kernel_version():
    res = subprocess.check_output("uname -a", shell=True)

    try:
        ret = re.search(r"([0-9]+\.[0-9]+\.[0-9]+)", str(res)).group(1)
    except:
        print("Couldn't guess the kernel version. Please specify it manually.")
        raise

    return ret


def run_tracing(ift):
    probes = ift.build_probes()
    opts = ift.build_opts()
    b = BPF(text=probes, cflags=opts)
    events = b["events"]

    flows = {}

    def handle_event(cpu, data, size):
        event = cast(data, POINTER(EventData)).contents
        event_name = ift.resolve_event_name(event.event_id)

        if str(event.l3_protocol) == L3_PROTO_TO_ID["IPv4"]:
            saddr = ipaddress.IPv4Address(socket.ntohl(event.addrs.v4.saddr))
            daddr = ipaddress.IPv4Address(socket.ntohl(event.addrs.v4.daddr))
        elif str(event.l3_protocol) == L3_PROTO_TO_ID["IPv6"]:
            saddr = ipaddress.IPv6Address(bytes(event.addrs.v6.saddr))
            daddr = ipaddress.IPv6Address(bytes(event.addrs.v6.daddr))
        else:
            print(f"Unsupported l3 protocol {event.l3_protocol}")
            return

        flow = Flow(
            l3_protocol=ID_TO_L3_PROTO[str(event.l3_protocol)],
            l4_protocol=ID_TO_L4_PROTO[str(event.l4_protocol)],
            saddr=str(saddr),
            daddr=str(daddr),
            sport=socket.ntohs(event.sport),
            dport=socket.ntohs(event.dport),
        )

        event_list = flows.get(flow, [])
        if event_name not in event_list:
            event_list.append(event_name)

        flows[flow] = event_list

    events.open_perf_buffer(handle_event, page_cnt=64)

    print("Trace ready!")
    while 1:
        try:
            b.perf_buffer_poll()
        except KeyboardInterrupt:
            exit(0)

        for f, e in flows.items():
            proto = f.l4_protocol
            src = f.saddr + ((":" + str(f.sport)) if f.sport != 0 else "")
            dst = f.daddr + ((":" + str(f.dport)) if f.dport != 0 else "")
            print(f"{proto}\t{src}\t->\t{dst}\t{e}")


@click.command()
@click.option("--kernel-version", default=guess_kernel_version, type=str, help="Specify Linux kernel version")
@click.option("--l3proto", default="any", type=click.Choice(["any", "4", "6"]), help="Specify IP version")
@click.option("--l4proto", default="any", help="Specify L4 protocol")
@click.option("--saddr4", default="any", help="Specify IPv4 source address")
@click.option("--daddr4", default="any", help="Specify IPv4 destination address")
@click.option("--saddr6", default="any", help="Specify IPv6 source address")
@click.option("--daddr6", default="any", help="Specify IPv6 destination address")
@click.option("--sport", default="any", help="Specify source port number")
@click.option("--dport", default="any", help="Specify destination port number")
@click.option("--list", is_flag=True, help="List available groups and functions")
def main(kernel_version, l3proto, l4proto, saddr4, daddr4, saddr6, daddr6, sport, dport, list):
    """
    Track the journey of the packets in Linux L3 layer
    """
    ift = IPFTracer(
        kernel_version=kernel_version,
        l3proto=l3proto,
        l4proto=l4proto,
        saddr4=saddr4,
        daddr4=daddr4,
        saddr6=saddr6,
        daddr6=daddr6,
        sport=sport,
        dport=dport
    )

    if list:
        ift.list_functions()
        exit(0)

    init_ethertypes_mapping()
    init_protocol_mapping()

    run_tracing(ift)


if __name__ == "__main__":
    main()