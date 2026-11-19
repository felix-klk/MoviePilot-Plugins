import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath, pathToFileURL } from 'node:url'

const require = createRequire(new URL('../plugins.v3/subtitlemanualupload/package.json', import.meta.url))
const { createServer } = require('vite')
const { default: vuePlugin } = await import(pathToFileURL(require.resolve('@vitejs/plugin-vue')))
const { createSSRApp, defineComponent, h, ref } = require('vue')
const { renderToString } = require('vue/server-renderer')
const root = fileURLToPath(new URL('../plugins.v3/subtitlemanualupload', import.meta.url))
const server = await createServer({
  root, configFile: false, plugins: [vuePlugin()],
  server: { middlewareMode: true, ws: false }, appType: 'custom',
  optimizeDeps: { noDiscovery: true },
})
const previousWindow = globalThis.window
let now = 0
let nextTimer = 0
const timers = new Map()
globalThis.window = {
  setTimeout(callback, delay) {
    const id = ++nextTimer
    timers.set(id, { callback, at: now + delay })
    return id
  },
  clearTimeout(id) { timers.delete(id) },
}

async function advance(milliseconds) {
  now += milliseconds
  for (const [id, timer] of timers) {
    if (timer.at <= now) {
      timers.delete(id)
      timer.callback()
    }
  }
  await Promise.resolve()
  await Promise.resolve()
}

try {
  const base = '/src'
  const { useOnlineSubtitles } = await server.ssrLoadModule(`${base}/composables/useOnlineSubtitles.js`)
  const { useUploadPreview } = await server.ssrLoadModule(`${base}/composables/useUploadPreview.js`)
  const targets = [{ id: 'movie', basename: 'Pressure' }]
  const message = ref('')
  const upload = useUploadPreview({
    pluginApi: ref({}), unwrapResponse: response => response.data,
    errorMessage: error => error.message, error: ref(''), message,
    selectedTargets: ref(targets), visibleTargets: ref(targets),
    selectedMedia: ref({}), selectedSeason: ref('all'),
    isLocked: () => false, isStreamTarget: () => false,
    timelineAvailable: ref(false), compactTargetName: target => target.basename,
    buildOutputName: () => 'Pressure.zh-CN.ass',
  })
  upload.prepareOnlineUploadState(targets, 'Pressure')
  let finishDownload
  const online = useOnlineSubtitles({
    pluginApi: ref({ onlineDownloadPreview: () => new Promise(resolve => { finishDownload = resolve }) }),
    unwrapResponse: response => response.data, errorMessage: error => error.message,
    error: ref(''), message, selectedMedia: ref({}), selectedTargets: ref(targets),
    selectedSeason: ref('all'), batchUploadTargets: ref(targets),
    lockedTargetPayload: () => ({}), aiAvailable: ref(false),
    openOnlinePreview: upload.openOnlinePreview,
  })
  online.onlineTargets.value = targets
  online.onlineResults.value = [{ provider: 'subhd', result_id: '7z' }, { provider: 'subhd', result_id: 'ass' }]
  online.selectedOnlineResultIds.value = ['subhd:7z', 'subhd:ass']
  online.onlineDialog.value = true
  const pending = online.downloadOnlinePreview()
  await advance(35000)
  assert.equal(online.onlineDownloading.value, true)
  await advance(23000)
  const data = {
    session_id: 'partial-preview', source: 'online',
    items: [{ upload_id: 'ass', source_name: '1782049523828.ass', target_id: 'movie', ext: '.ass', language_suffix: 'chi' }],
    invalid_files: [{ name: '1784636833932.7z', reason: '7z 压缩包解压需要容器内可执行 unar' }],
    unsupported_files: [],
  }
  finishDownload({ data })
  await pending
  assert.equal(online.onlineError.value, '')
  assert.equal(online.onlineDialog.value, false)
  assert.equal(upload.uploadDialog.value, true)
  assert.equal(upload.preview.value.items[0].selected, true)
  assert.equal(upload.preview.value.invalid_files[0].reason, data.invalid_files[0].reason)
  assert.equal(timers.size, 0)

  online.onlineDialog.value = true
  const timedOut = online.downloadOnlinePreview()
  await advance(360001)
  await timedOut
  assert.match(online.onlineError.value, /超时/)
  assert.equal(online.onlineDialog.value, true)
  finishDownload({ data: { ...data, session_id: 'expired' } })
  await Promise.resolve()
  assert.equal(upload.preview.value.session_id, 'partial-preview')

  const cancelled = online.downloadOnlinePreview()
  online.stopOnlineDownload()
  finishDownload({ data: { ...data, session_id: 'cancelled' } })
  await cancelled
  assert.equal(upload.preview.value.session_id, 'partial-preview')
  assert.equal(online.onlineDialog.value, true)
  assert.equal(timers.size, 0)

  const { default: UploadDialog } = await server.ssrLoadModule(`${base}/components/UploadDialog.vue`)
  const app = createSSRApp(UploadDialog, {
    modelValue: true, hasPreviewItems: true, preview: data, uploadTargets: targets,
    formatBytes: String, rarDependencyModeLabel: String, buildOutputName: () => 'Pressure.zh-CN.ass',
  })
  const source = await readFile(new URL('../plugins.v3/subtitlemanualupload/src/components/UploadDialog.vue', import.meta.url), 'utf8')
  const stub = defineComponent({
    props: ['text'],
    setup(props, { slots }) { return () => h('div', [props.text, slots.default?.()]) },
  })
  for (const name of new Set(source.match(/\bV[A-Z][A-Za-z]+/g))) app.component(name, stub)
  const html = await renderToString(app)
  assert.match(html, /1782049523828\.ass/)
  assert.match(html, /1784636833932\.7z/)
  assert.match(html, /容器内可执行 unar/)
  assert.match(html, /可继续选择已解析的字幕/)
  console.log('Delayed partial preview, visible failure reasons, timeout and cancellation passed')

  const official = { provider: 'subhd', result_id: 'official', title: 'Pressure 官方简中', subtitle_type: '官方字幕', download_count: 1200 }
  const ai = { provider: 'subhd', result_id: 'ai', title: 'Pressure AI 校对', subtitle_type: 'AI校对', download_count: 600 }
  const prediction = {
    enabled: true,
    min_score: 20,
    plans: [{
      target_ids: ['movie'], target: 'Pressure', scope: 'single', candidate_count: 2, rejections: {},
      candidates: [
        { ...official, auto_rank: { release: '片源匹配', subtitle_type: '官方字幕', match: 60, download_bonus: 2, time_bonus: 1, total: 63 } },
        { ...ai, auto_rank: { release: '片源匹配', subtitle_type: 'AI校对', match: 62, download_bonus: 2, time_bonus: 0, total: 64 } },
      ],
    }],
    skipped: [],
  }
  let previewCalls = 0
  const search = useOnlineSubtitles({
    pluginApi: ref({
      onlineSearchProvider: async () => ({ data: { results: [official, ai] } }),
      onlineAutoPreview: async payload => {
        previewCalls++
        assert.deepEqual(payload.results.map(item => item.result_id), ['official', 'ai'])
        return { data: prediction }
      },
    }),
    unwrapResponse: response => response.data, errorMessage: error => error.message,
    error: ref(''), message, selectedMedia: ref({}), selectedTargets: ref(targets),
    selectedSeason: ref('all'), batchUploadTargets: ref(targets),
    lockedTargetPayload: () => [], aiAvailable: ref(false),
  })
  search.onlineTargets.value = targets
  search.onlineSelectedProviders.value = ['subhd']
  await search.runOnlineSearch()
  await new Promise(resolve => setImmediate(resolve))
  assert.equal(previewCalls, 1)
  assert.equal(search.onlineAutoPreview.value.plans[0].candidates[0].result_id, 'official')
  search.onlineResultFilters.value.type = 'AI校对'
  search.selectedOnlineResultIds.value = ['subhd:official']
  assert.deepEqual(search.filteredOnlineResults.value.map(item => item.result_id), ['ai'])
  assert.equal(previewCalls, 1)

  const { default: OnlineSubtitleDialog } = await server.ssrLoadModule(`${base}/components/OnlineSubtitleDialog.vue`)
  const { onlineResultKey, isOnlineResultDownloadable } = await server.ssrLoadModule(`${base}/utils/onlineResult.js`)
  const dialogSource = await readFile(new URL('../plugins.v3/subtitlemanualupload/src/components/OnlineSubtitleDialog.vue', import.meta.url), 'utf8')
  const dialog = createSSRApp(OnlineSubtitleDialog, {
    modelValue: true, hasOnlineResults: true, onlineResults: search.onlineResults.value,
    onlineResultFilters: search.onlineResultFilters.value, onlineAutoPreview: search.onlineAutoPreview.value,
    filteredOnlineResults: search.filteredOnlineResults.value, selectedOnlineResults: search.selectedOnlineResults.value,
    providerName: String, providerProgressColor: String, providerProgressText: String,
    onlineResultMeta: () => '', onlineResultKey, isOnlineResultDownloadable,
  })
  for (const name of new Set(dialogSource.match(/\bV[A-Z][A-Za-z]+/g))) dialog.component(name, stub)
  const dialogHtml = await renderToString(dialog)
  assert.match(dialogHtml, /首选候选已被当前筛选隐藏/)
  assert.match(dialogHtml, /含隐藏的 1 条/)
  assert.match(dialogHtml, /Pressure 官方简中/)
  assert.match(dialogHtml, /基础 60 · 下载量 \+2/)
  assert.match(dialogHtml, /下载量升序/)
  console.log('Search prediction uses the full result batch and the dialog shows hidden selections and ranking details')
} finally {
  globalThis.window = previousWindow
  await server.close()
}
