#!/bin/bash
set -e

# Directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

# Create tmp directory if it doesn't exist
mkdir -p "${SCRIPT_DIR}/tmp"

# Clean up any previous telemetry files
rm -f "${SCRIPT_DIR}/tmp/telemetry_data.jsonl"

# Start collect_telemetry.py in the background
echo "Starting telemetry collection..."
TELEMETRY_OUTPUT="${SCRIPT_DIR}/tmp/telemetry_data.jsonl"
nohup python3 "${SCRIPT_DIR}/collect_telemetry.py" --proc-path /proc --output-file "${TELEMETRY_OUTPUT}" --sampling-rate 2 > "${SCRIPT_DIR}/tmp/collect.log" 2>&1 &
TELEMETRY_PID=$!

echo "Telemetry collection started with PID: ${TELEMETRY_PID}"
echo "Output file: ${TELEMETRY_OUTPUT}"

# Function to cleanup on exit
cleanup() {
    echo "Stopping telemetry collection..."
    kill -9 ${TELEMETRY_PID} 2>/dev/null || true
    wait ${TELEMETRY_PID} 2>/dev/null || true
    echo "Telemetry collection stopped."
}

# Set trap to ensure cleanup on script exit
trap cleanup EXIT

# Simulate CPU load
echo "Simulating CPU load..."
for i in {1..3}; do
    dd if=/dev/zero of=/dev/null bs=1M count=4096 &
    CPU_PIDS[${i}]=$!
done
sleep 15

# Kill CPU load processes
for pid in ${CPU_PIDS[*]}; do
    kill -9 $pid 2>/dev/null || true
done

# Simulate memory load
echo "Simulating memory load..."
python3 -c '
import array
# Allocate ~500MB of memory
data = array.array("i", [0] * (500 * 1024 * 1024 // 4))
print(f"Allocated ~500MB of memory")
import time
time.sleep(10)
' &
MEMORY_PID=$!
sleep 15

# Kill memory process if still running
kill -9 ${MEMORY_PID} 2>/dev/null || true

# Simulate disk load
echo "Simulating disk load..."
dd if=/dev/zero of="${SCRIPT_DIR}/tmp/largefile" bs=1M count=1024
sleep 5
rm "${SCRIPT_DIR}/tmp/largefile"

# Simulate network load
echo "Simulating network load..."
for i in {1..10}; do
    curl -s -o /dev/null https://github.com &
    sleep 1
done

# Wait for all simulations to complete
echo "Waiting for all simulations to complete..."
sleep 10

# Stop telemetry collection
echo "Stopping telemetry collection..."
kill -9 ${TELEMETRY_PID} 2>/dev/null || true
wait ${TELEMETRY_PID} 2>/dev/null || true

# Process telemetry data
echo "Processing telemetry data..."
python3 "${SCRIPT_DIR}/process_telemetry.py" --input-file "${TELEMETRY_OUTPUT}" --generate-chart true --github-summary "${SCRIPT_DIR}/tmp/summary.md"

# Show results
echo "Test completed. Results are available in:"
echo "- Raw data: ${TELEMETRY_OUTPUT}"
echo "- Charts: tmp/cpu_chart.png, tmp/memory_chart.png, etc."
echo "- Summary: tmp/summary.md"

# Display summary.md if it exists
if [ -f "${SCRIPT_DIR}/tmp/summary.md" ]; then
    echo ""
    echo "==================== SUMMARY ===================="
    cat "${SCRIPT_DIR}/tmp/summary.md"
    echo "================================================="
fi

echo ""
echo "To view PNG charts, you can copy them to a location where you can view them"
echo "or use a tool like 'display' if you have ImageMagick installed."
