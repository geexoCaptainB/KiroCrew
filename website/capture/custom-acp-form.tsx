/**
 * Evidence for the Custom ACP harness on Developer > Agent Backend.
 *
 * THE CHANGE: the picker lists a `custom` row that stays visible while it is
 * unselectable (its remedy is the operator's, not a policy denial), and
 * highlighting it reveals an owner-only form -- command, arguments, permission
 * option, permission value -- that writes `agent.custom_acp` whole through the
 * config PATCH and then re-checks the row.
 *
 * The scene mounts the REAL `AgentBackendTab` from `src/` against the real
 * stylesheet, theme tokens and live i18n catalog, with only `fetch` stubbed to
 * answer what the gateway answers. Nothing here re-implements the row, the form
 * or any string, so a frame proves what ships.
 *
 *   ?scene=empty       no `agent.custom_acp` yet: the row reads "not set up" and
 *                      the form is blank
 *   ?scene=configured  a complete record on disk: the row is selectable and
 *                      installed, the form shows the stored values
 *   ?patch_delay_ms=N  hold the PATCH open for N ms, so the "Saving…" state has
 *                      a frame to be caught in
 *   ?theme=dark|light
 *
 * The stub is STATEFUL across one page: a PATCH to `agent.custom_acp` becomes the
 * record every later GET answers with, the schema starts offering `custom`, and the
 * probe row flips to selectable+installed -- which is what the gateway does after
 * the config load the write triggers. So driving the empty scene through the form
 * (fill -> Save -> Saved) walks the same states the panel walks in production, and
 * the save-flow frames document a sequence rather than three unrelated stills.
 */
import { createRoot } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'

import { store } from '../src/store'
import { ThemeProvider } from '../src/hooks/useTheme'
import { UIModeProvider } from '../src/hooks/useUIMode'
import { ZoomProvider } from '../src/hooks/ZoomProvider'
import { AgentBackendTab } from '../src/pages/developer/AgentBackendTab'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const scene = (params.get('scene') || 'empty') as 'empty' | 'configured'
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const patchDelayMs = Math.max(0, Number(params.get('patch_delay_ms') || 0) || 0)

document.documentElement.dataset.mode = theme
document.documentElement.dataset.theme = theme === 'light' ? 'kiro-light' : 'kiro-dark'

initI18n()

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })

const CUSTOM_ACP = {
  command: 'my-acp',
  args: ['serve', '--stdio'],
  gate_option: 'mode',
  gate_value: 'read-only',
}

/** The Kiro CLI row every install carries, so the list has its default. */
const KIRO_ROW = {
  id: '',
  policy_id: 'kiro',
  selectable: true,
  installed: 'installed',
  missing_components: [],
  install_command: '',
  restart_required: false,
  capabilities: [
    { id: 'crew_tools', available: true },
    { id: 'mid_turn_steer', available: true },
  ],
  security_notes: [],
  operator_notes: [],
  tool_approval: 'agent_spec',
  offered_by_build: true,
}

/** The custom row as the probe reports it before and after a record exists. */
const CUSTOM_ROWS = {
  empty: {
    id: 'custom',
    policy_id: 'custom',
    selectable: false,
    installed: 'missing',
    missing_components: ['agent.custom_acp'],
    install_command: 'set agent.custom_acp in config.json',
    restart_required: false,
    capabilities: [
      { id: 'crew_tools', available: false },
      { id: 'mid_turn_steer', available: false },
    ],
    security_notes: [],
    operator_notes: [],
    tool_approval: 'unverified',
    offered_by_build: true,
    configurable: true,
  },
  configured: {
    id: 'custom',
    policy_id: 'custom',
    selectable: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    capabilities: [
      { id: 'crew_tools', available: false },
      { id: 'mid_turn_steer', available: false },
    ],
    security_notes: [],
    operator_notes: [],
    tool_approval: 'session_config',
    offered_by_build: true,
    configurable: true,
  },
}

/**
 * What is "on disk" right now. Seeded from the scene and rewritten by a PATCH, so
 * the answers below follow the write the way the gateway's do after its reload.
 */
let stored: typeof CUSTOM_ACP | undefined = scene === 'configured' ? CUSTOM_ACP : undefined
const configured = () => typeof stored?.command === 'string' && stored.command.trim() !== ''
const customRow = () => CUSTOM_ROWS[configured() ? 'configured' : 'empty']

const wait = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  const method = (init?.method || 'GET').toUpperCase()
  if (url.includes('/api/config/schema')) {
    return Promise.resolve(
      json({
        entries: [
          {
            path: 'agent.acp_backend',
            type: 'enum',
            enumValues: configured() ? ['', 'claude', 'custom'] : ['', 'claude'],
          },
        ],
      }),
    )
  }
  if (url.includes('/api/config/kirocrew')) {
    if (method === 'PATCH') {
      // The form writes the record WHOLE under one key; take it as the new disk
      // state once the (optionally held) request completes.
      const body = init?.body
        ? (JSON.parse(String(init.body)) as { path?: string; value?: unknown })
        : {}
      return wait(patchDelayMs).then(() => {
        if (body.path === 'agent.custom_acp') stored = body.value as typeof CUSTOM_ACP
        return json({ ok: true })
      })
    }
    return Promise.resolve(
      json({
        agent: configured() ? { acp_backend: '', custom_acp: stored } : { acp_backend: '' },
      }),
    )
  }
  if (url.includes('/api/acp-backends/recheck')) {
    return Promise.resolve(json({ backend: customRow() }))
  }
  if (url.includes('/api/acp-backends')) {
    return Promise.resolve(json({ backends: [KIRO_ROW, customRow()] }))
  }
  // The Kiro sign-in card renders only while KAS is offered, which this list
  // does not, so nothing else is asked for.
  return Promise.resolve(json({}, 404))
}) as typeof fetch

// Retries would keep the panel in its skeleton for the whole capture window;
// the settled state is the frame under test.
const qc = new QueryClient({
  defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
})

const root = createRoot(document.getElementById('root')!)
root.render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <ThemeProvider>
          <UIModeProvider>
            <ZoomProvider>
              <div
                data-capture-root
                style={{
                  maxWidth: 960,
                  margin: '0 auto',
                  padding: 24,
                  background: 'var(--bg)',
                  minHeight: 480,
                }}
              >
                <AgentBackendTab />
              </div>
            </ZoomProvider>
          </UIModeProvider>
        </ThemeProvider>
      </MemoryRouter>
    </QueryClientProvider>
  </Provider>,
)
