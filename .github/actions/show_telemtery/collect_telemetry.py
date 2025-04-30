#!/usr/bin/env python3
# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Telemetry collection script that samples system statistics from /proc
"""
import argparse
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# 100MB in KB
memory_threshold_kb = 100 * 1024

# CPU usage threshold (1%)
cpu_threshold = 1.0


class ProcTelemetryCollector:
    def __init__(self, proc_path, output_file, sampling_rate):
        self.proc_path = Path(proc_path)
        self.output_file = output_file
        self.sampling_rate = sampling_rate
        self.running = True

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, self.handle_signal)
        signal.signal(signal.SIGINT, self.handle_signal)

    def handle_signal(self, signum, frame):
        print(f"Received signal {signum}, shutting down...")
        self.running = False

    def collect_memory_by_process(self):
        """Collect memory usage by process from /proc/[pid]/status"""
        processes = {}

        for pid_dir in self.proc_path.glob("[0-9]*"):
            try:
                pid = int(pid_dir.name)
                status_file = pid_dir / "status"
                cmdline_file = pid_dir / "cmdline"

                if not status_file.exists() or not cmdline_file.exists():
                    continue

                # Get process name and memory usage
                with open(status_file, "r") as f:
                    status_data = f.read()

                name_match = re.search(r"Name:\s+(.+)", status_data)
                rss_match = re.search(r"VmRSS:\s+(\d+)", status_data)

                if name_match and rss_match:
                    name = name_match.group(1).strip()
                    rss_kb = int(rss_match.group(1))

                    # Filter out processes with memory usage below threshold
                    if rss_kb < memory_threshold_kb:
                        continue

                    # Try to get command line
                    try:
                        with open(cmdline_file, "r") as f:
                            cmdline = f.read().replace("\0", " ").strip()
                    except:
                        cmdline = name

                    processes[pid] = {
                        "pid": pid,
                        "name": name,
                        "cmdline": cmdline[:100] if cmdline else name,  # Truncate long command lines
                        "memory_kb": rss_kb,
                    }
            except (ValueError, IOError, OSError) as e:
                # Skip processes that can't be read
                pass

        return list(processes.values())

    def collect_cpu_by_process(self):
        """Collect CPU usage by process from /proc/[pid]/stat"""
        processes = {}
        total_cpu_usage = 0

        # Read system uptime
        try:
            with open(self.proc_path / "uptime", "r") as f:
                uptime = float(f.read().split()[0])
        except (IOError, OSError):
            uptime = 0

        # Read CPU stats for each process
        for pid_dir in self.proc_path.glob("[0-9]*"):
            try:
                pid = int(pid_dir.name)
                stat_file = pid_dir / "stat"

                if not stat_file.exists():
                    continue

                with open(stat_file, "r") as f:
                    stat = f.read()

                # Parse the stat file
                stat_values = stat.split()
                if len(stat_values) < 22:
                    continue

                # Process name is the 2nd field, but it's surrounded by parentheses
                name = stat_values[1].strip("()")

                # CPU time calculation from stat fields
                utime = int(stat_values[13])  # User time
                stime = int(stat_values[14])  # System time
                cutime = int(stat_values[15])  # User time of children
                cstime = int(stat_values[16])  # System time of children
                starttime = int(stat_values[21])  # Start time

                total_time = utime + stime + cutime + cstime
                seconds_running = uptime - (starttime / os.sysconf(os.sysconf_names["SC_CLK_TCK"]))

                # Calculate CPU usage percentage (this is an approximation)
                if seconds_running > 0:
                    cpu_usage = 100 * ((total_time / os.sysconf(os.sysconf_names["SC_CLK_TCK"])) / seconds_running)
                else:
                    cpu_usage = 0

                # Filter out processes with CPU usage below threshold
                if cpu_usage < cpu_threshold:
                    continue

                processes[pid] = {"pid": pid, "name": name, "cpu_percent": round(cpu_usage, 2)}

                total_cpu_usage += cpu_usage
            except (ValueError, IOError, OSError):
                # Skip processes that can't be read
                pass

        return list(processes.values())

    def collect_disk_space(self):
        """Collect free disk space from /proc/mounts and statvfs"""
        disk_info = []

        try:
            with open(self.proc_path / "mounts", "r") as f:
                mounts = f.readlines()

            for mount in mounts:
                parts = mount.split()
                if len(parts) >= 2:
                    device = parts[0]
                    mountpoint = parts[1]

                    # Skip pseudo filesystems
                    if device.startswith("/dev/") and os.path.exists(mountpoint):
                        try:
                            stat = os.statvfs(mountpoint)
                            free_bytes = stat.f_bfree * stat.f_bsize
                            total_bytes = stat.f_blocks * stat.f_bsize

                            disk_info.append(
                                {
                                    "device": device,
                                    "mountpoint": mountpoint,
                                    "free_bytes": free_bytes,
                                    "total_bytes": total_bytes,
                                    "free_percent": round((free_bytes / total_bytes) * 100, 2)
                                    if total_bytes > 0
                                    else 0,
                                }
                            )
                        except OSError:
                            pass
        except (IOError, OSError):
            pass

        return disk_info

    def collect_network_stats(self):
        """Collect network statistics from /proc/net/dev"""
        network_stats = []

        try:
            with open(self.proc_path / "net" / "dev", "r") as f:
                lines = f.readlines()

            # Skip header lines
            for line in lines[2:]:
                parts = line.split(":")
                if len(parts) >= 2:
                    interface = parts[0].strip()
                    if interface != "lo":  # Skip loopback
                        values = parts[1].split()
                        if len(values) >= 16:
                            network_stats.append(
                                {
                                    "interface": interface,
                                    "bytes_recv": int(values[0]),
                                    "packets_recv": int(values[1]),
                                    "bytes_sent": int(values[8]),
                                    "packets_sent": int(values[9]),
                                }
                            )
        except (IOError, OSError):
            pass

        return network_stats

    def collect_system_load(self):
        """Collect system load from /proc/loadavg"""
        try:
            with open(self.proc_path / "loadavg", "r") as f:
                loadavg = f.read().split()

            return {"load_1min": float(loadavg[0]), "load_5min": float(loadavg[1]), "load_15min": float(loadavg[2])}
        except (IOError, OSError, IndexError):
            return {"load_1min": 0, "load_5min": 0, "load_15min": 0}

    def collect_memory_stats(self):
        """Collect system memory statistics from /proc/meminfo (only essentials)"""
        mem_total = 0
        mem_free = 0

        try:
            with open(self.proc_path / "meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        parts = line.split(":")
                        if len(parts) == 2:
                            value_parts = parts[1].strip().split()
                            if len(value_parts) >= 1:
                                mem_total = int(value_parts[0])
                    elif line.startswith("MemFree:"):
                        parts = line.split(":")
                        if len(parts) == 2:
                            value_parts = parts[1].strip().split()
                            if len(value_parts) >= 1:
                                mem_free = int(value_parts[0])
        except (IOError, OSError):
            pass

        # Calculate only the essential metrics
        mem_used = mem_total - mem_free
        mem_used_percent = round((mem_used / mem_total) * 100, 2) if mem_total > 0 else 0

        return {"MemTotal": mem_total, "MemUsed": mem_used, "MemUsedPercent": mem_used_percent}

    def sample_all(self):
        """Sample all system statistics and return as a dict"""
        timestamp = datetime.now().isoformat()

        return {
            "timestamp": timestamp,
            "processes_memory": self.collect_memory_by_process(),
            "processes_cpu": self.collect_cpu_by_process(),
            "disk_space": self.collect_disk_space(),
            "network": self.collect_network_stats(),
            "system_load": self.collect_system_load(),
            "memory_stats": self.collect_memory_stats(),
        }

    def run(self):
        """Run the collection loop, writing samples to the output file"""
        print(f"Starting telemetry collection. Sampling every {self.sampling_rate} seconds.")
        print(f"Writing data to {self.output_file}")

        # Create output directory if it doesn't exist
        output_dir = os.path.dirname(self.output_file)
        if output_dir and not os.path.exists(output_dir):
            try:
                os.makedirs(output_dir, exist_ok=True)
                print(f"Created output directory: {output_dir}")
            except OSError as e:
                print(f"Warning: Could not create output directory {output_dir}: {e}")

        # Delete the old telemetry file if it exists
        if os.path.exists(self.output_file):
            try:
                os.remove(self.output_file)
                print(f"Deleted existing telemetry file: {self.output_file}")
            except OSError as e:
                print(f"Warning: Could not delete existing telemetry file {self.output_file}: {e}")

        while self.running:
            try:
                # Sample all metrics
                sample = self.sample_all()

                # Write to output file
                with open(self.output_file, "a") as f:
                    f.write(json.dumps(sample) + "\n")
                    f.flush()

                # Sleep for the sampling interval
                time.sleep(self.sampling_rate)
            except Exception as e:
                print(f"Error during collection: {e}")
                time.sleep(self.sampling_rate)  # Still sleep to avoid tight loop if error persists

        print("Telemetry collection stopped.")


def main():
    parser = argparse.ArgumentParser(description="Collect system telemetry from /proc")
    parser.add_argument("--proc-path", required=True, help="Path to proc directory")
    parser.add_argument("--sampling-rate", type=int, default=5, help="Sampling rate in seconds")
    parser.add_argument("--output-file", required=True, help="Output file path for telemetry data")

    args = parser.parse_args()

    collector = ProcTelemetryCollector(
        proc_path=args.proc_path, output_file=args.output_file, sampling_rate=args.sampling_rate
    )

    try:
        collector.run()
    except KeyboardInterrupt:
        print("Collection stopped by user.")
        sys.exit(0)


if __name__ == "__main__":
    main()
