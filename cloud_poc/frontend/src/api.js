const apiBase = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000'

async function call(path, { method = 'GET', body, apiKey } = {}) {
  const headers = { 'Content-Type': 'application/json' }
  if (apiKey) headers['X-Pyfuse-API-Key'] = apiKey
  const response = await fetch(`${apiBase}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    let detail = ''
    try {
      const data = await response.json()
      detail = data.detail || ''
    } catch {
      detail = await response.text()
    }
    throw new Error(detail || `Request failed (${response.status})`)
  }
  return response.json()
}

export const api = {
  register: (email, password) => call('/api/v1/users/register', {
    method: 'POST', body: { email, password },
  }),
  login: (email, password) => call('/api/v1/users/login', {
    method: 'POST', body: { email, password },
  }),
  me: (apiKey) => call('/api/v1/users/me', { apiKey }),
  summary: (apiKey) => call('/api/v1/usage/summary', { apiKey }),
  tasks: (apiKey, limit = 25) => call(`/api/v1/usage/tasks?limit=${limit}`, { apiKey }),
}
