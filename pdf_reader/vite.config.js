import { defineConfig } from 'vite'
import { spawn } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '..')

function readBody(req) {
  return new Promise((resolveBody, reject) => {
    let body = ''
    req.setEncoding('utf8')
    req.on('data', (chunk) => {
      body += chunk
    })
    req.on('end', () => resolveBody(body))
    req.on('error', reject)
  })
}

function runVectorApi(payload) {
  return new Promise((resolveResult, reject) => {
    const child = spawn('python', [resolve(repoRoot, 'pdf_parser', 'vector_api.py')], {
      cwd: repoRoot,
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => {
      stdout += chunk
    })
    child.stderr.on('data', (chunk) => {
      stderr += chunk
    })
    child.on('error', reject)
    child.on('close', (code) => {
      if (code !== 0) {
        reject(new Error(stderr || stdout || `vector_api.py exited with ${code}`))
        return
      }
      try {
        resolveResult(JSON.parse(stdout))
      } catch (error) {
        reject(error)
      }
    })
    child.stdin.end(JSON.stringify(payload))
  })
}

function vectorSnifferPlugin() {
  return {
    name: 'vector-sniffer-api',
    configureServer(server) {
      server.middlewares.use('/api/vector-sniffer', async (req, res) => {
        if (req.method !== 'POST') {
          res.statusCode = 405
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ ok: false, error: 'Method not allowed' }))
          return
        }
        try {
          const body = await readBody(req)
          const payload = JSON.parse(body || '{}')
          if (typeof payload.pdf_url === 'string') {
            payload.pdf_path = resolve(here, 'public', payload.pdf_url.replace(/^\//, ''))
          }
          const result = await runVectorApi(payload)
          res.statusCode = result.ok ? 200 : 500
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify(result))
        } catch (error) {
          res.statusCode = 500
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ ok: false, error: error instanceof Error ? error.message : String(error) }))
        }
      })
    },
  }
}

export default defineConfig({
  plugins: [vectorSnifferPlugin()],
})
