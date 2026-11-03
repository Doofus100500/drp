require "json"
require "open3"
require "pathname"

def below(root, relative)
  raise "Invalid path" if relative.start_with?("/") || relative.split("/").any? { |part| ["", ".", ".."].include?(part) }
  path = File.join(root, relative)
  raise "Path escapes root" unless File.realpath(path).start_with?(File.realpath(root) + "/")
  path
end

def git(path, *args)
  output, status = Open3.capture2e("/opt/gitlab/embedded/bin/git", "--git-dir=#{path}", *args)
  raise "Git check failed" unless status.success?
  output
end

begin
  catalog = JSON.parse(STDIN.read)
  count = 0
  catalog.fetch("repositories").each do |entry|
    next if entry.fetch("state") == "absent"

    storage = entry.fetch("storage")
    raise "Invalid storage" unless storage.match?(/\A[a-zA-Z0-9_.-]+\z/) && ![".", ".."].include?(storage)
    path = below("/var/opt/gitlab/git-data/#{storage}/repositories", entry.fetch("relative_path"))
    raise "Not a bare repository" unless git(path, "rev-parse", "--is-bare-repository").strip == "true"
    git(path, "fsck", "--full")
    refs = below("/var/opt/gitlab/backups/repositories", entry.fetch("steps").last.fetch("ref_path"))
    expected = File.readlines(refs, chomp: true).reject { |line| line.split.last == "HEAD" }.sort
    actual = git(path, "for-each-ref", "--format=%(objectname) %(refname)").lines.map(&:strip).sort
    raise "References do not match backup" unless actual == expected
    count += 1
  end
  puts "DRP_REPOSITORIES_VERIFIED=#{count}"
rescue StandardError => error
  warn "DRP_REPOSITORY_VERIFICATION_FAILED: #{error.class}: #{error.message}"
  exit 1
end
