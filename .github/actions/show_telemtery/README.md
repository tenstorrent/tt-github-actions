# System Telemetry Collector

This GitHub Action collects system statistics by sampling from the Linux `/proc` filesystem and generates visual reports of resource usage during workflow execution.

## What This Action Does

The System Telemetry Collector:

1. Starts a background process that samples system statistics at a configurable interval
2. Collects detailed metrics including:
   - Memory usage per process
   - CPU load per process
   - Free disk space
   - Network bandwidth usage
   - Overall system load
3. Generates visual charts in the GitHub job summary
4. Optionally uploads raw telemetry data as an artifact

This action is particularly useful for:
- Monitoring resource usage of CI/CD workflows
- Debugging performance issues in build or test processes
- Understanding the resource footprint of your applications during testing
- Collecting benchmarking data across different environments

## How to Use in Your Workflow

Add this action to your workflow YAML file:

```yaml
jobs:
  telemetry-test:
    runs-on: ubuntu-latest
    steps:
      # Start telemetry collection
      - name: Start System Telemetry
        uses: ./.github/actions/show_telemtery
        with:
          proc_path: '/proc'  # Use '/host/proc' in Docker containers where host proc is mounted
          sampling_rate: '5'  # Sample every 5 seconds
          generate_chart: 'true'
          upload_artifact: 'true'
          artifact_name: 'system-telemetry'

      # Your other workflow steps here...
      - name: Run your process
        run: |
          echo "Running resource-intensive process..."
          # Your commands here
          sleep 30  # Example process

      # If you've set upload_artifact to true, you should add this step
      - name: Upload telemetry data
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: system-telemetry
          path: ${{ env.TELEMETRY_ARTIFACT_PATH }}
          if-no-files-found: ignore
```

### Input Parameters

| Parameter | Description | Required | Default |
|-----------|-------------|----------|---------|
| `proc_path` | Path to the proc folder | Yes | `/proc` |
| `sampling_rate` | Sampling rate in seconds | No | `5` |
| `generate_chart` | Generate charts in GitHub job summary | No | `true` |
| `upload_artifact` | Upload telemetry data as an artifact | No | `true` |
| `artifact_name` | Name of the artifact to upload | No | `system-telemetry` |

## Using in Docker Container Environments

When running in a Docker container with the host's `/proc` directory mounted:

```yaml
jobs:
  docker-telemetry:
    runs-on: ubuntu-latest
    container:
      image: your-docker-image
      volumes:
        - /proc:/host/proc:ro  # Mount host proc as read-only
    steps:
      - name: Start System Telemetry
        uses: ./.github/actions/show_telemtery
        with:
          proc_path: '/host/proc'  # Point to mounted host proc
          sampling_rate: '2'        # More frequent sampling

      # Your workflow steps here

      # Don't forget to add this if you want the artifact
      - name: Upload telemetry data
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: system-telemetry
          path: ${{ env.TELEMETRY_ARTIFACT_PATH }}
          if-no-files-found: ignore
```

## Testing Locally

To test changes to this action locally:

1. **Clone the repository**:
   ```bash
   git clone <your-repo-url>
   cd <your-repo-directory>/.github/actions/show_telemtery
   ```

2. **Install dependencies**:
   ```bash
   python3 -m pip install -r requirements.txt
   ```

3. **Run the collection script manually**:
   ```bash
   # Using nohup to keep process running even if terminal closes
   # and redirecting output to a log file
   nohup python3 collect_telemetry.py \
     --proc-path "/proc" \
     --sampling-rate 5 \
     --output-file "tmp/telemetry_data.jsonl" > telemetry.log 2>&1 &

   # The process is now running in the background with PID shown
   echo "Process started with PID: $!"
   ```

4. **Stop the collection** (in another terminal):
   ```bash
   # Find the PID of the python process
   ps aux | grep collect_telemetry

   # Kill the process
   kill <PID>
   ```

5. **Process the collected data**:
   ```bash
   python3 process_telemetry.py \
     --input-file "tmp/telemetry_data.jsonl" \
     --generate-chart "true" \
     --github-summary "tmp/summary.md"
   ```

6. **View the results**:
   ```bash
   cat /tmp/summary.md  # View the markdown content
   # Open any generated chart_*.png files
   ```

## Example: Running with an Act Tool

If you use [Act](https://github.com/nektos/act) to test GitHub Actions locally:

```bash
# Create a minimal workflow for testing
cat > .github/workflows/test-telemetry.yml << 'EOF'
name: Test Telemetry
on: workflow_dispatch
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - name: Collect Telemetry
        uses: ./.github/actions/show_telemtery
        with:
          proc_path: '/proc'
      - name: Run Test Process
        run: |
          # Create some activity
          dd if=/dev/zero of=/tmp/test bs=1M count=100
          rm /tmp/test
      - name: Upload telemetry data
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: system-telemetry
          path: ${{ env.TELEMETRY_ARTIFACT_PATH }}
          if-no-files-found: ignore
EOF

# Run the workflow with act
act -j test
```

## Data Format

The telemetry data is stored in JSON Lines format (JSONL), with each line containing a complete sample. Each sample includes:

```json
{
  "timestamp": "2023-01-01T12:00:00.123456",
  "processes_memory": [...],
  "processes_cpu": [...],
  "disk_space": [...],
  "network": [...],
  "system_load": {...},
  "memory_stats": {...}
}
```

## How It Works

This action uses GitHub's `pre` and `post` hooks to automatically start telemetry collection before your workflow steps begin and process the results after they complete. Dependencies are managed through a `requirements.txt` file. Here's what happens:

1. **Pre-Hook**: Before your workflow steps run, the action:
   - Installs required Python dependencies
   - Starts a background process to collect telemetry from the specified proc path
   - Records the process ID in the GitHub environment

2. **Your Workflow**: Your workflow steps run normally while telemetry is collected in the background

3. **Post-Hook**: After your workflow completes (or fails), the action:
   - Stops the telemetry collection process
   - Processes the collected data
   - Generates charts in the job summary
   - Prepares telemetry data for artifact upload and sets the `TELEMETRY_ARTIFACT_PATH` environment variable

## Troubleshooting

- **No data collected**: Ensure the action has appropriate permissions to read from the `/proc` path
- **Charts not appearing**: Check that matplotlib and dependencies are installed correctly
- **Empty charts**: Ensure the workflow runs long enough to collect multiple samples
- **Missing artifacts**: Make sure you've added the upload-artifact step shown in the examples above

## Contributing

Contributions are welcome! When developing changes:

1. Follow the local testing procedure described above
2. Ensure all Python dependencies are properly declared in the `requirements.txt` file
3. Test your changes in a workflow before submitting a PR
