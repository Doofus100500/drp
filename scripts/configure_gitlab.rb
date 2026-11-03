# Executed only after restoring the database, before publishing any ports.
require "json"

begin
  entries = JSON.parse(STDIN.read).fetch("repositories")
  project_ids = entries.select { |e| e["kind"] == "project" }.map { |e| e.fetch("project_id") }.sort
  snippet_ids = entries.select { |e| e["kind"] == "snippet" }.map { |e| e.fetch("container_id") }.sort
  raise "Project inventory mismatch" unless Project.order(:id).pluck(:id) == project_ids
  raise "Snippet inventory mismatch" unless Snippet.order(:id).pluck(:id) == snippet_ids
  Gitlab::CurrentSettings.update!(
    password_authentication_enabled_for_web: true,
    password_authentication_enabled_for_git: true,
    silent_mode_enabled: true
  )
  Ci::Runner.update_all(active: false)
  Ci::PipelineSchedule.update_all(active: false)
  ProjectFeature.update_all(builds_access_level: 0)
  RemoteMirror.update_all(enabled: false)
  # Pull mirrors are an EE feature; the column can also exist in CE databases.
  Project.update_all(mirror: false) if Project.column_names.include?("mirror")
  Integration.update_all(active: false)
  ContainerExpirationPolicy.update_all(enabled: false)
  raise "Password login disabled" unless Gitlab::CurrentSettings.password_authentication_enabled_for_web
  raise "Git password login disabled" unless Gitlab::CurrentSettings.password_authentication_enabled_for_git
  raise "Silent mode disabled" unless Gitlab::CurrentSettings.silent_mode_enabled
  # Force an actual query against the restored database.
  puts "DRP_CONFIGURED users=#{User.count} projects=#{Project.count}"
rescue StandardError => error
  # The orchestrator captures this only in its root-owned restore.log.
  warn "DRP_CONFIGURATION_FAILED: #{error.class}: #{error.message}"
  exit 1
end
