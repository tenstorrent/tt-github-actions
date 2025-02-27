# Job id action

This action removes unused software packages from Github runner to make space for build.

It works in case when we run Github runners with "container" option and we get disc layout simliar to this.

```
Filesystem      Size  Used Avail Use% Mounted on
overlay          72G   63G  8.7G  88% /
tmpfs            64M     0   64M   0% /dev
shm              64M     0   64M   0% /dev/shm
/dev/root        72G   63G  8.7G  88% /__w
tmpfs           1.6G  1.2M  1.6G   1% /run/docker.sock
tmpfs           3.9G     0  3.9G   0% /proc/acpi
tmpfs           3.9G     0  3.9G   0% /proc/scsi
tmpfs           3.9G     0  3.9G   0% /sys/firmware
```

Removing packages can get us additional 6-7GB of storage space

```
Filesystem      Size  Used Avail Use% Mounted on
overlay          72G   57G   16G  79% /
tmpfs            64M     0   64M   0% /dev
shm              64M     0   64M   0% /dev/shm
/dev/root        72G   57G   16G  79% /__w
tmpfs           1.6G  1.2M  1.6G   1% /run/docker.sock
tmpfs           3.9G     0  3.9G   0% /proc/acpi
tmpfs           3.9G     0  3.9G   0% /proc/scsi
tmpfs           3.9G     0  3.9G   0% /sys/firmware
```

## Basic usage

```
- name: Maximize space
  uses: tenstorrent/tt-github-actions/.github/actions/maximize_space
```
