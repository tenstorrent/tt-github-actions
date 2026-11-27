# Copy the org.opencontainers.image.* labels that every platform image carries
# with the same value onto the index as annotations. Annotations already on the
# index win. An index with nothing to add is left byte-for-byte unchanged.
#
# Input: the index. $labels: one config.Labels object per platform image.

($labels | map(with_entries(select(.key | startswith("org.opencontainers.image."))))) as $ls
| ($ls[0] // {}
   | with_entries(.key as $k | .value as $v | select(all($ls[]; .[$k] == $v)))) as $common
| if ($common | length) > 0 then .annotations = $common + (.annotations // {}) else . end
