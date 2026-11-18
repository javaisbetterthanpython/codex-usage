# Lessons

## PyPI tokens are project-scoped
- A token scoped to one PyPI project gets a 403 when uploading any other project.
- A brand-new PyPI project needs its first upload done with an account-wide token. After that, switch to a token scoped to the new project.
- Rule: before promising a PyPI release of a new project, check which project the available token is scoped to.
