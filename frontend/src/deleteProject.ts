import { api } from './api'
import type { Project } from './api'

/** Ask the user to type the project name, then delete it. Resolves true when the project was deleted. */
export async function confirmAndDeleteProject(project: Project): Promise<boolean> {
  const typed = window.prompt(
    `Delete “${project.name}”?\n\nThis permanently removes its sessions, uploaded files, discovery runs and generated files. ` +
    'Jira tickets already sent to ETL Crew are not affected. The audit log keeps a record of the deletion.\n\n' +
    'Type the project name to confirm:')
  if (typed === null) return false
  if (typed.trim() !== project.name) {
    window.alert('The name did not match, so nothing was deleted.')
    return false
  }
  await api.deleteProject(project.id)
  return true
}
