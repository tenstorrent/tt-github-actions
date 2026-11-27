#!/usr/bin/env ruby
# frozen_string_literal: true

# Composite actions cannot share input definitions, so docker-publish and
# docker-bake-publish each declare the gate inputs. This check fails when the
# copies drift apart. Run from anywhere: ruby .github/actions/check-consistency.rb
#
# Checked:
#   - an input declared by both actions has the same description and default
#   - the env both `gate` steps pass to gate.sh agrees on every shared key
#   - tool-images.json is well formed and is the only place an image digest
#     appears: both scanner inputs default to empty and no script or
#     action.yml hardcodes one
#   - every `uses:` is pinned to a full commit sha
#   - every variable a step's scripts read is set in that step's env

require 'json'
require 'yaml'

ROOT = __dir__
PUBLISH = File.join(ROOT, 'docker-publish', 'action.yml')
BAKE = File.join(ROOT, 'docker-bake-publish', 'action.yml')
PINS = File.join(ROOT, 'tool-images.json')
MIRROR_PREFIX = 'ghcr.io/tenstorrent/'

# Differ on purpose: each step reads its report id from its own earlier step,
# and only docker-publish lets a caller turn the SBOM off.
GATE_ENV_PER_ACTION = %w[REPORT_ROOT SBOM].freeze

errors = []
publish = YAML.safe_load(File.read(PUBLISH))
bake = YAML.safe_load(File.read(BAKE))

shared = publish['inputs'].keys & bake['inputs'].keys
shared.each do |name|
  %w[description default required].each do |field|
    a = publish['inputs'][name][field]
    b = bake['inputs'][name][field]
    next if a == b

    errors << "input '#{name}': #{field} differs\n    docker-publish:      #{a.inspect}\n    docker-bake-publish: #{b.inspect}"
  end
end

gate_env = lambda do |action|
  step = action['runs']['steps'].find { |s| s['id'] == 'gate' }
  step ? step['env'] : {}
end
publish_env = gate_env.call(publish)
bake_env = gate_env.call(bake)
errors << 'a gate step is missing or has no env' if publish_env.empty? || bake_env.empty?
((publish_env.keys & bake_env.keys) - GATE_ENV_PER_ACTION).each do |key|
  next if publish_env[key] == bake_env[key]

  errors << "gate env '#{key}' differs: #{publish_env[key].inspect} vs #{bake_env[key].inspect}"
end
# Everything gate.sh needs from the bake action must also be passed by docker-publish.
(bake_env.keys - publish_env.keys).each do |key|
  errors << "gate env '#{key}' is passed by docker-bake-publish only"
end

# tool-images.json is the only place a tool image digest may live.
pins = JSON.parse(File.read(PINS))
errors << "#{File.basename(PINS)} lists no images" if pins.empty?
pins.each do |name, pin|
  %w[source tag digest mirror].each do |key|
    errors << "tool-images.json '#{name}': missing #{key}" if pin[key].to_s.empty?
  end
  unless pin['digest'].to_s.match?(/\Asha256:[0-9a-f]{64}\z/)
    errors << "tool-images.json '#{name}': digest is not a sha256 digest: #{pin['digest'].inspect}"
  end
  unless pin['mirror'].to_s.start_with?(MIRROR_PREFIX)
    errors << "tool-images.json '#{name}': mirror must be under #{MIRROR_PREFIX}, got #{pin['mirror'].inspect}"
  end
end
errors << "tool-images.json has no 'buildkit-syft-scanner', which both actions default to" unless pins.key?('buildkit-syft-scanner')

{ 'sbom-generator' => publish, 'syft-scanner-image' => bake }.each do |input, action|
  default = action['inputs'][input]['default'].to_s
  errors << "input '#{input}' must default to empty (the pin file supplies it), got #{default.inspect}" unless default.empty?
end
(Dir[File.join(ROOT, '{docker-publish,docker-bake-publish}', '**', '*.{sh,yml}'), File.join(ROOT, 'mirror-tool-images.sh')] - [File.join(ROOT, 'docker-publish', 'scripts', 'install-tools.sh')]).each do |file|
  next unless File.read(file).match?(/@sha256:[0-9a-f]{64}/)

  errors << "#{file.delete_prefix("#{ROOT}/")}: hardcodes an image digest; pin images in tool-images.json"
end

{ PUBLISH => publish, BAKE => bake }.each do |path, action|
  action['runs']['steps'].each do |step|
    uses = step['uses']
    next if uses.nil? || uses.start_with?('./')
    next if uses.match?(/@[0-9a-f]{40}\z/)

    errors << "#{path.delete_prefix("#{ROOT}/")}: '#{uses}' is not pinned to a full commit sha"
  end
end

# A variable a script reads but its step does not set is inherited from the
# caller's job env, so a job-level SBOM: "false" or INSTALL_SYFT: "false" could
# turn off part of the mandatory pipeline. A script's reads include those of
# the scripts it runs, minus what it assigns before running them.
SCRIPTS = File.join(ROOT, 'docker-publish', 'scripts')
RUNNER_VARS = /\A(GITHUB|RUNNER|ACTIONS)_/
script_reads = lambda do |file, seen|
  next [] if seen.include?(file)

  seen << file
  text = File.read(file)
  children = text.scan(%r{/([\w-]+\.sh)"}).flatten.map { |f| File.join(SCRIPTS, f) } - [file]
  assigned = text.scan(/\b([A-Z][A-Z0-9_]*)=/).flatten
  text.scan(/\$\{([A-Z][A-Z0-9_]*):?[-?]/).flatten |
    (children.flat_map { |c| script_reads.call(c, seen) } - assigned)
end
{ PUBLISH => publish, BAKE => bake }.each do |path, action|
  action['runs']['steps'].each do |step|
    run = step['run'].to_s
    run.scan(%r{\$\{GITHUB_ACTION_PATH\}(/[\w./-]+\.sh)}).flatten.each do |rel|
      script = File.expand_path(File.join(File.dirname(path), rel))
      missing = script_reads.call(script, []).grep_v(RUNNER_VARS) -
                (step['env'] || {}).keys - run.scan(/\b([A-Z][A-Z0-9_]*)=/).flatten
      missing.each do |var|
        errors << "#{path.delete_prefix("#{ROOT}/")} step '#{step['name']}': #{File.basename(script)} reads #{var}, which the step's env does not set"
      end
    end
  end
end

if errors.empty?
  puts "ok: #{shared.size} shared inputs, gate env, tool image pins and action pins are consistent"
else
  warn errors.map { |e| "- #{e}" }.join("\n")
  exit 1
end
