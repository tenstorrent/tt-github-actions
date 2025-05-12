#!/usr/bin/env python3
# SPDX-FileCopyrightText: (c) 2025 Tenstorrent AI ULC
#
# SPDX-License-Identifier: Apache-2.0
"""
Process telemetry data collected by collect_telemetry.py and generate charts
for GitHub job summary.
"""
import argparse
import json
import os
import requests
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any, Tuple, Optional


class TelemetryProcessor:
    def __init__(self, input_file):
        self.input_file = input_file
        self.data = self.load_data()

    def load_data(self):
        """Load telemetry data from JSONL file."""
        data = []
        try:
            with open(self.input_file, "r") as f:
                for line in f:
                    try:
                        sample = json.loads(line.strip())
                        data.append(sample)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            print(f"Warning: Telemetry file {self.input_file} not found")

        return data

    def get_timestamps(self):
        """Extract timestamps from all samples."""
        timestamps = []
        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                timestamps.append(ts)
            except (KeyError, ValueError):
                continue
        return timestamps

    def process_memory_usage(self):
        """Process memory usage data over time."""
        timestamps = []
        total_memory = []
        used_memory = []
        used_percent = []

        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                mem_stats = sample.get("memory_stats", {})

                mem_total = mem_stats.get("MemTotal", 0) / 1024  # Convert to MB
                mem_used = mem_stats.get("MemUsed", 0) / 1024  # Convert to MB
                mem_percent = mem_stats.get("MemUsedPercent", 0)

                timestamps.append(ts)
                total_memory.append(mem_total)
                used_memory.append(mem_used)
                used_percent.append(mem_percent)
            except (KeyError, ValueError):
                continue

        return {
            "timestamps": timestamps,
            "total_memory_mb": total_memory,
            "used_memory_mb": used_memory,
            "used_percent": used_percent,
        }

    def process_top_processes_by_memory(self, top_n=5):
        """Process top N processes by memory usage over time."""
        # Collect process memory usage by name
        process_memory = defaultdict(list)
        timestamps = []

        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                timestamps.append(ts)

                # Dictionary to hold total memory for each process in this sample
                sample_process_memory = defaultdict(int)

                for proc in sample.get("processes_memory", []):
                    name = proc.get("name", "unknown")
                    memory_kb = proc.get("memory_kb", 0)
                    sample_process_memory[name] += memory_kb

                # Add memory usage for each process in this sample
                for name, memory in sample_process_memory.items():
                    process_memory[name].append(memory / 1024)  # Convert to MB
            except (KeyError, ValueError):
                continue

        # Determine top N processes by average memory usage
        process_avg_memory = {}
        for name, memory_values in process_memory.items():
            # Pad with zeros if the process wasn't present in all samples
            padded_values = memory_values + [0] * (len(timestamps) - len(memory_values))
            process_avg_memory[name] = sum(padded_values) / len(padded_values) if padded_values else 0

        top_processes = sorted(process_avg_memory.keys(), key=lambda x: process_avg_memory[x], reverse=True)[:top_n]

        # Prepare return data with only top processes
        result = {"timestamps": timestamps}
        for name in top_processes:
            # Pad with zeros for any missing values
            padded_values = process_memory[name] + [0] * (len(timestamps) - len(process_memory[name]))
            result[name] = padded_values

        return result

    def process_cpu_load(self):
        """Process CPU load data over time."""
        timestamps = []
        load_1min = []
        load_5min = []
        load_15min = []

        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                load = sample.get("system_load", {})

                timestamps.append(ts)
                load_1min.append(load.get("load_1min", 0))
                load_5min.append(load.get("load_5min", 0))
                load_15min.append(load.get("load_15min", 0))
            except (KeyError, ValueError):
                continue

        return {"timestamps": timestamps, "load_1min": load_1min, "load_5min": load_5min, "load_15min": load_15min}

    def process_top_processes_by_cpu(self, top_n=5):
        """Process top N processes by CPU usage over time."""
        process_cpu = defaultdict(list)
        timestamps = []

        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                timestamps.append(ts)

                # Dictionary to hold CPU usage for each process in this sample
                sample_process_cpu = defaultdict(float)

                for proc in sample.get("processes_cpu", []):
                    name = proc.get("name", "unknown")
                    cpu_percent = proc.get("cpu_percent", 0)
                    sample_process_cpu[name] += cpu_percent

                # Add CPU usage for each process in this sample
                for name, cpu in sample_process_cpu.items():
                    process_cpu[name].append(cpu)
            except (KeyError, ValueError):
                continue

        # Determine top N processes by average CPU usage
        process_avg_cpu = {}
        for name, cpu_values in process_cpu.items():
            # Pad with zeros if the process wasn't present in all samples
            padded_values = cpu_values + [0] * (len(timestamps) - len(cpu_values))
            process_avg_cpu[name] = sum(padded_values) / len(padded_values) if padded_values else 0

        top_processes = sorted(process_avg_cpu.keys(), key=lambda x: process_avg_cpu[x], reverse=True)[:top_n]

        # Prepare return data with only top processes
        result = {"timestamps": timestamps}
        for name in top_processes:
            # Pad with zeros for any missing values
            padded_values = process_cpu[name] + [0] * (len(timestamps) - len(process_cpu[name]))
            result[name] = padded_values

        return result

    def process_network_usage(self):
        """Process network usage data over time."""
        timestamps = []
        bytes_recv = []
        bytes_sent = []
        interfaces = set()

        # First pass: identify all interfaces
        for sample in self.data:
            for net_stat in sample.get("network", []):
                interfaces.add(net_stat.get("interface", ""))

        # Create structure for interface data
        interface_data = {interface: {"bytes_recv": [], "bytes_sent": []} for interface in interfaces}

        # Second pass: collect data for each interface
        last_bytes = {interface: {"recv": 0, "sent": 0} for interface in interfaces}

        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                timestamps.append(ts)

                # Reset counters for this sample
                sample_bytes_recv = 0
                sample_bytes_sent = 0

                # Track interfaces seen in this sample
                seen_interfaces = set()

                for net_stat in sample.get("network", []):
                    interface = net_stat.get("interface", "")
                    if interface and interface in interfaces:
                        seen_interfaces.add(interface)

                        # Calculate differences to handle counter resets
                        current_recv = net_stat.get("bytes_recv", 0)
                        current_sent = net_stat.get("bytes_sent", 0)

                        # Simple difference, doesn't handle counter resets perfectly
                        recv_diff = max(0, current_recv - last_bytes[interface]["recv"])
                        sent_diff = max(0, current_sent - last_bytes[interface]["sent"])

                        # Update last known values
                        last_bytes[interface]["recv"] = current_recv
                        last_bytes[interface]["sent"] = current_sent

                        # Store values for this interface
                        interface_data[interface]["bytes_recv"].append(recv_diff)
                        interface_data[interface]["bytes_sent"].append(sent_diff)

                        # Add to totals
                        sample_bytes_recv += recv_diff
                        sample_bytes_sent += sent_diff

                # Handle interfaces not seen in this sample
                for interface in interfaces - seen_interfaces:
                    interface_data[interface]["bytes_recv"].append(0)
                    interface_data[interface]["bytes_sent"].append(0)

                # Add totals
                bytes_recv.append(sample_bytes_recv)
                bytes_sent.append(sample_bytes_sent)

            except (KeyError, ValueError):
                continue

        # Convert to KB/s based on timestamps
        if len(timestamps) > 1:
            time_diffs = [(timestamps[i + 1] - timestamps[i]).total_seconds() for i in range(len(timestamps) - 1)]
            avg_interval = sum(time_diffs) / len(time_diffs) if time_diffs else 1

            # Convert bytes to KB/s
            bytes_recv = [b / 1024 / avg_interval for b in bytes_recv]
            bytes_sent = [b / 1024 / avg_interval for b in bytes_sent]

            for interface in interfaces:
                interface_data[interface]["bytes_recv"] = [
                    b / 1024 / avg_interval for b in interface_data[interface]["bytes_recv"]
                ]
                interface_data[interface]["bytes_sent"] = [
                    b / 1024 / avg_interval for b in interface_data[interface]["bytes_sent"]
                ]

        return {
            "timestamps": timestamps,
            "total_recv_kbps": bytes_recv,
            "total_sent_kbps": bytes_sent,
            "interfaces": interface_data,
        }

    def process_disk_space(self):
        """Process disk space usage over time."""
        timestamps = []
        mountpoints = set()

        # First pass: identify all mountpoints
        for sample in self.data:
            for disk_stat in sample.get("disk_space", []):
                mountpoints.add(disk_stat.get("mountpoint", ""))

        # Create structure for mountpoint data
        mountpoint_data = {mp: {"free_gb": [], "total_gb": [], "free_percent": []} for mp in mountpoints}

        # Second pass: collect data for each mountpoint
        for sample in self.data:
            try:
                ts = datetime.fromisoformat(sample["timestamp"])
                timestamps.append(ts)

                # Track mountpoints seen in this sample
                seen_mountpoints = set()

                for disk_stat in sample.get("disk_space", []):
                    mp = disk_stat.get("mountpoint", "")
                    if mp and mp in mountpoints:
                        seen_mountpoints.add(mp)

                        free_bytes = disk_stat.get("free_bytes", 0)
                        total_bytes = disk_stat.get("total_bytes", 0)
                        free_percent = disk_stat.get("free_percent", 0)

                        mountpoint_data[mp]["free_gb"].append(free_bytes / (1024**3))  # Convert to GB
                        mountpoint_data[mp]["total_gb"].append(total_bytes / (1024**3))  # Convert to GB
                        mountpoint_data[mp]["free_percent"].append(free_percent)

                # Handle mountpoints not seen in this sample
                for mp in mountpoints - seen_mountpoints:
                    # Use the last known values if available, or zeros
                    if mountpoint_data[mp]["free_gb"]:
                        mountpoint_data[mp]["free_gb"].append(mountpoint_data[mp]["free_gb"][-1])
                        mountpoint_data[mp]["total_gb"].append(mountpoint_data[mp]["total_gb"][-1])
                        mountpoint_data[mp]["free_percent"].append(mountpoint_data[mp]["free_percent"][-1])
                    else:
                        mountpoint_data[mp]["free_gb"].append(0)
                        mountpoint_data[mp]["total_gb"].append(0)
                        mountpoint_data[mp]["free_percent"].append(0)

            except (KeyError, ValueError):
                continue

        return {"timestamps": timestamps, "mountpoints": mountpoint_data}

    def generate_memory_chart(self):
        """Generate a chart for memory usage."""
        memory_data = self.process_memory_usage()

        if not memory_data["timestamps"]:
            return None, "No memory data available"

        # Data is already prepared by process_memory_usage
        return memory_data, "System memory usage over time"

    def generate_top_processes_memory_chart(self, top_n=5):
        """Generate a chart for top processes by memory usage."""
        process_memory = self.process_top_processes_by_memory(top_n)

        if not process_memory["timestamps"] or len(process_memory) <= 1:
            return None, "No process memory data available"

        # Data is already prepared by process_top_processes_by_memory
        return process_memory, f"Top {top_n} processes by memory usage"

    def generate_cpu_load_chart(self):
        """Generate a chart for CPU load."""
        cpu_data = self.process_cpu_load()

        if not cpu_data["timestamps"]:
            return None, "No CPU load data available"

        # Data is already prepared by process_cpu_load
        return cpu_data, "System load average over time"

    def generate_top_processes_cpu_chart(self, top_n=5):
        """Generate a chart for top processes by CPU usage."""
        process_cpu = self.process_top_processes_by_cpu(top_n)

        if not process_cpu["timestamps"] or len(process_cpu) <= 1:
            return None, "No process CPU data available"

        # Data is already prepared by process_top_processes_by_cpu
        return process_cpu, f"Top {top_n} processes by CPU usage"

    def generate_network_chart(self):
        """Generate a chart for network usage."""
        network_data = self.process_network_usage()

        if not network_data["timestamps"]:
            return None, "No network data available"

        # Convert to format suitable for Globadge
        chart_data = {
            "timestamps": network_data["timestamps"],
            "Received (KB/s)": network_data["total_recv_kbps"],
            "Sent (KB/s)": network_data["total_sent_kbps"],
        }

        return chart_data, "Network bandwidth usage over time"

    def generate_disk_chart(self):
        """Generate a chart for disk space usage."""
        disk_data = self.process_disk_space()

        if not disk_data["timestamps"] or not disk_data["mountpoints"]:
            return None, "No disk space data available"

        # Convert to format suitable for Globadge
        chart_data = {"timestamps": disk_data["timestamps"]}

        # Add each mountpoint as a series
        for mp, data in disk_data["mountpoints"].items():
            # Only include mountpoints with data
            if data["free_percent"] and any(p > 0 for p in data["free_percent"]):
                chart_data[f"{mp} (% Free)"] = data["free_percent"]

        return chart_data, "Disk free space over time"

    def generate_all_charts(self):
        """Generate all charts and return them as a list of (fig, description) tuples."""
        charts = []

        # Memory charts
        mem_chart = self.generate_memory_chart()
        if mem_chart[0]:
            charts.append(mem_chart)

        proc_mem_chart = self.generate_top_processes_memory_chart()
        if proc_mem_chart[0]:
            charts.append(proc_mem_chart)

        # CPU charts
        cpu_chart = self.generate_cpu_load_chart()
        if cpu_chart[0]:
            charts.append(cpu_chart)

        proc_cpu_chart = self.generate_top_processes_cpu_chart()
        if proc_cpu_chart[0]:
            charts.append(proc_cpu_chart)

        # Network chart
        net_chart = self.generate_network_chart()
        if net_chart[0]:
            charts.append(net_chart)

        # Disk chart
        disk_chart = self.generate_disk_chart()
        if disk_chart[0]:
            charts.append(disk_chart)

        return charts

    def generate_globadge_chart(
        self, chart_data: Dict[str, Any], title: str, x_label: str, y_label: str
    ) -> Optional[str]:
        """Generate a chart using the Globadge API and return the URL to the hosted chart.

        Args:
            chart_data: Dictionary containing the chart data in a format suitable for conversion to Globadge format
            title: Title of the chart
            x_label: Label for the x-axis
            y_label: Label for the y-axis

        Returns:
            URL to the hosted chart, or None if generation failed
        """
        # Prepare the data for the Globadge API
        if "timestamps" not in chart_data or len(chart_data["timestamps"]) == 0:
            return None

        # Configure the request for Globadge API
        url = "https://api.globadge.com/v1/chartgen/line/time"

        # Convert timestamps to epoch milliseconds
        timestamps = [(ts.timestamp() * 1000) for ts in chart_data["timestamps"]]

        # Prepare lines data
        lines = []
        # High contrast colors that work well on both light and dark themes
        colors = ["#ff3333", "#3366ff", "#33cc33", "#cc33cc", "#ff9900", "#00ccff", "#cc6600", "#ff00ff", "#333333"]

        color_index = 0
        for key, values in chart_data.items():
            if key != "timestamps" and len(values) > 0:
                # Limit to 9 lines (Globadge limitation)
                if color_index >= 9:
                    break

                points = []
                for i, value in enumerate(values):
                    if i < len(timestamps):
                        points.append({"x": timestamps[i], "y": max(0, float(value))})

                lines.append({"label": key, "color": colors[color_index], "points": points})
                color_index += 1

        # Skip if no data lines
        if not lines:
            return None

        line_color = "#ff7f00"
        # Build request payload
        payload = {
            "options": {
                "width": 800,
                "height": 500,
                "xAxis": {"label": x_label, "color": line_color},
                "yAxis": {"label": y_label, "color": line_color},
                "timeTicks": {"unit": "minute", "interval": 5, "format": "%H:%M"},
                "chart": {"padding": 20},  # Add padding around the chart
            },
            "lines": lines,
        }

        try:
            # Make the API request
            response = requests.put(url, json=payload)
            response.raise_for_status()

            # Extract the chart URL from the response
            result = response.json()
            if "url" in result:
                return result["url"]
            else:
                print(f"Warning: Globadge API response missing URL: {result}")
                return None

        except Exception as e:
            print(f"Warning: Failed to generate chart via Globadge API: {e}")
            return None

    def generate_chart_url(self, chart_data, description) -> Optional[str]:
        """Generate a chart URL using the Globadge API.

        Args:
            chart_data: Dictionary with the chart data
            description: Description of the chart

        Returns:
            URL to the hosted chart, or None if generation failed
        """
        if not chart_data or not chart_data.get("timestamps"):
            return None

        # Determine chart type and appropriate y-axis label
        x_label = "Time"
        y_label = ""

        if "memory usage" in description.lower():
            y_label = "Memory (MB / %)"
        elif "load average" in description.lower():
            y_label = "Load Average"
        elif "processes by cpu" in description.lower():
            y_label = "CPU (%)"
        elif "processes by memory" in description.lower():
            y_label = "Memory (MB)"
        elif "network" in description.lower():
            y_label = "KB/s"
        elif "disk" in description.lower():
            y_label = "Free Space (%)"

        # Generate chart URL
        return self.generate_globadge_chart(chart_data, description, x_label, y_label)

    def write_github_summary(self, github_summary_path):
        """Write charts and summary to GitHub job summary markdown file."""
        if not self.data:
            # Create directory for summary if it doesn't exist
            summary_dir = os.path.dirname(github_summary_path)
            if summary_dir and not os.path.exists(summary_dir):
                try:
                    os.makedirs(summary_dir, exist_ok=True)
                    print(f"Created summary directory: {summary_dir}")
                except OSError as e:
                    print(f"Warning: Could not create summary directory {summary_dir}: {e}")

            with open(github_summary_path, "a") as f:
                f.write("## System Telemetry\n\n")
                f.write("No telemetry data was collected.\n")
            return

        # Calculate collection period
        timestamps = self.get_timestamps()
        start_time = min(timestamps) if timestamps else None
        end_time = max(timestamps) if timestamps else None
        duration = (end_time - start_time).total_seconds() if start_time and end_time else 0

        # Generate all charts
        charts = self.generate_all_charts()

        # Write summary to GitHub job summary
        with open(github_summary_path, "w") as f:
            f.write("## System Telemetry\n\n")

            # Collection details
            f.write("### Collection Details\n\n")
            f.write(f"- **Samples collected**: {len(self.data)}\n")
            if start_time and end_time:
                f.write(
                    f"- **Collection period**: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                )
                f.write(f"- **Duration**: {int(duration)} seconds\n")
                if len(self.data) > 1:
                    f.write(f"- **Average sampling rate**: {duration / (len(self.data) - 1):.2f} seconds\n")
            f.write("\n")

            # Add charts using Globadge API
            for i, (chart_data, description) in enumerate(charts):
                if chart_data:
                    # Generate chart URL using Globadge API
                    chart_url = self.generate_chart_url(chart_data, description)

                    f.write(f"### {description}\n\n")

                    if chart_url:
                        # Add the chart image with pure markdown syntax (no need for div wrapper anymore)
                        f.write(f"![{description}]({chart_url})\n\n")
                    else:
                        # Display text if chart generation failed
                        f.write(f"*Chart generation failed. Please check the logs for more information.*\n\n")

    def generate_summary(self):
        """Generate a text summary of the collected data."""
        if not self.data:
            return "No telemetry data was collected."

        # Calculate collection period
        timestamps = self.get_timestamps()
        start_time = min(timestamps) if timestamps else None
        end_time = max(timestamps) if timestamps else None
        duration = (end_time - start_time).total_seconds() if start_time and end_time else 0

        summary = ["## Telemetry Summary"]

        # Collection details
        summary.append("\n### Collection Details")
        summary.append(f"- Samples collected: {len(self.data)}")
        if start_time and end_time:
            summary.append(
                f"- Collection period: {start_time.strftime('%Y-%m-%d %H:%M:%S')} to {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
            )
            summary.append(f"- Duration: {int(duration)} seconds")
            if len(self.data) > 1:
                summary.append(f"- Average sampling rate: {duration / (len(self.data) - 1):.2f} seconds")

        # Memory summary
        memory_data = self.process_memory_usage()
        if memory_data["timestamps"]:
            avg_mem_used = sum(memory_data["used_memory_mb"]) / len(memory_data["used_memory_mb"])
            avg_mem_percent = sum(memory_data["used_percent"]) / len(memory_data["used_percent"])
            summary.append("\n### Memory Usage")
            summary.append(f"- Average memory used: {avg_mem_used:.2f} MB ({avg_mem_percent:.2f}%)")

        # CPU summary
        cpu_data = self.process_cpu_load()
        if cpu_data["timestamps"]:
            avg_load_1min = sum(cpu_data["load_1min"]) / len(cpu_data["load_1min"])
            summary.append("\n### CPU Usage")
            summary.append(f"- Average 1-minute load: {avg_load_1min:.2f}")

        # Network summary
        network_data = self.process_network_usage()
        if network_data["timestamps"]:
            avg_recv = sum(network_data["total_recv_kbps"]) / len(network_data["total_recv_kbps"])
            avg_sent = sum(network_data["total_sent_kbps"]) / len(network_data["total_sent_kbps"])
            summary.append("\n### Network Usage")
            summary.append(f"- Average receive: {avg_recv:.2f} KB/s")
            summary.append(f"- Average send: {avg_sent:.2f} KB/s")

        # Disk summary
        disk_data = self.process_disk_space()
        if disk_data["timestamps"] and disk_data["mountpoints"]:
            summary.append("\n### Disk Space")
            for mp, data in disk_data["mountpoints"].items():
                if data["free_percent"]:
                    avg_free = sum(data["free_percent"]) / len(data["free_percent"])
                    summary.append(f"- {mp}: {avg_free:.2f}% free")

        return "\n".join(summary)


def main():
    parser = argparse.ArgumentParser(description="Process telemetry data and generate charts")
    parser.add_argument("--input-file", required=True, help="Input file with telemetry data")
    parser.add_argument("--generate-chart", default="true", help="Whether to generate charts")
    parser.add_argument("--github-summary", default="", help="Path to GitHub job summary file")

    args = parser.parse_args()

    processor = TelemetryProcessor(args.input_file)

    # Print text summary
    print(processor.generate_summary())

    # Generate charts and write to GitHub summary if requested
    if args.generate_chart.lower() == "true" and args.github_summary:
        processor.write_github_summary(args.github_summary)


if __name__ == "__main__":
    main()
