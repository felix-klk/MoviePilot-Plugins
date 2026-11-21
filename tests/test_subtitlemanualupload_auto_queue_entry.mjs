import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(new URL('../plugins.v3/subtitlemanualupload/package.json', import.meta.url))
const { createServer } = require('vite')
const vuePlugin = require('@vitejs/plugin-vue')
const { createSSRApp, h, nextTick, ref } = require('vue')
const { renderToString } = require('vue/server-renderer')
const root = fileURLToPath(new URL('../plugins.v3/subtitlemanualupload', import.meta.url))
const server = await createServer({
  root,
  configFile: false,
  plugins: [vuePlugin()],
  server: { middlewareMode: true },
  appType: 'custom',
})

try {
  for (const variant of ['plugins.v2', 'plugins.v3']) {
    const base = fileURLToPath(new URL(`../${variant}/subtitlemanualupload/src`, import.meta.url))
    const { default: SearchPanel } = await server.ssrLoadModule(`${base}/components/MediaSearchPanel.vue`)
    const { useAutoTransferQueue } = await server.ssrLoadModule(`${base}/composables/useAutoTransferQueue.js`)
    const queue = useAutoTransferQueue({ pluginApi: ref({}), error: ref(''), message: ref('') })
    for (const [summary, expected] of [
      [{}, '暂无自动入库任务'],
      [{ pending: 2 }, '2 个排队'],
      [{ completed: 3 }, '3 个完成'],
      [{ failed: 1 }, '1 个失败'],
    ]) {
      queue.applyAutoTransferSummary(summary)
      const app = createSSRApp(SearchPanel, {
        rootTab: 'match',
        matchHistorySummary: '',
        indexSummary: '',
        autoQueueSummaryText: queue.autoQueueSummaryText.value,
      })
      // Render the real plugin component; only third-party display controls are stubbed.
      for (const [name, tag] of Object.entries({
        VCard: 'section', VCardText: 'div', VBtn: 'button', VTextField: 'input', VSelect: 'select',
      })) {
        app.component(name, { setup: (_, { slots }) => () => h(tag, null, slots.default?.()) })
      }
      const html = await renderToString(app)
      const buttons = [...html.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)].map(match => match[1])
      assert.ok(buttons.some(text => text.includes(`自动入库队列 · ${expected}`)), `${variant}: ${expected}`)
    }
    console.log(`${variant}: queue entry rendered for empty, pending, completed and failed states`)
  }
  const { useAutoTransferQueue } = await server.ssrLoadModule('/src/composables/useAutoTransferQueue.js')
  const { default: QueueDialog } = await server.ssrLoadModule('/src/components/AutoTransferQueueDialog.vue')
  const items = [
    { target_id: 'e02', label: 'S04E02', status: 'queued' },
    { target_id: 'e03', label: 'S04E03', status: 'locked' },
  ]
  const requests = []
  const receipt = '已加入 1 个补搜任务，1 个未加入'
  const queue = useAutoTransferQueue({
    pluginApi: ref({
      enqueueBilingualFollowup: async (payload) => {
        requests.push(payload)
        return { message: receipt, data: { followup_batch: { items } } }
      },
      autoTransferQueue: async () => ({ data: { summary: {}, tasks: [] } }),
    }),
    unwrapResponse: response => response.data,
    errorMessage: (err, fallback) => `${fallback}: ${err.message}`,
    error: ref(''),
    message: ref(''),
  })
  assert.equal(await queue.changeBilingualFollowup({ targetIds: ['e02', 'e03'], lockedTargetIds: ['e03'] }), true)
  assert.deepEqual(requests, [{ target_ids: ['e02', 'e03'], locked_target_ids: ['e03'] }])

  queue.autoQueueDialog.value = true
  await queue.loadAutoTransferQueue()
  const app = createSSRApp(QueueDialog, {
    batchFollowupResult: queue.batchFollowupResult.value,
    autoQueueTasks: [
      { id: 'written', status: 'completed', message: 'written', title: 'S04E04' },
      { id: 'existing', status: 'skipped', message: '目标已有符合语言要求的字幕', title: 'S04E05' },
    ],
  })
  for (const [name, tag] of Object.entries({
    VDialog: 'section', VCard: 'section', VCardTitle: 'header', VCardText: 'div',
    VBtn: 'button', VDivider: 'hr', VAlert: 'div', VTooltip: 'span',
  })) {
    app.component(name, {
      setup: (_, { slots }) => () => h(tag, null, slots.default?.() || slots.activator?.({ props: {} })),
    })
  }
  const html = await renderToString(app)
  assert.match(html, /text="已完成"/)
  assert.match(html, /auto-queue-message"[^>]*>已完成</)
  assert.match(html, /auto-queue-message"[^>]*>目标已有符合语言要求的字幕</)
  assert.ok(html.includes(receipt))
  assert.match(html, /S04E03.*目标已锁定/)
  queue.autoQueueDialog.value = false
  await nextTick()
  assert.equal(queue.batchFollowupResult.value, null)
} finally {
  await server.close()
}
