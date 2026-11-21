<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import '../styles/theme.css'
import { createJavSubtitleAssistantApi } from '../api/subtitleManualUploadApi'
import { errorMessage, unwrapResponse } from '../utils/formatters'
import { onlineProviderItems, defaultOnlineProviderIds } from '../utils/onlineResult'

const props = defineProps({
  api: { type: Object, default: null },
  pluginId: { type: String, default: 'JavSubtitleAssistant' },
  initialConfig: {
    type: Object,
    default: () => ({}),
  },
  saving: { type: Boolean, default: false },
})

const emit = defineEmits(['save', 'close'])
const configError = ref('')
const manualStrmPathPlaceholder = `/vol2/1000/raid/2/links2
/vol1/1000/media-strm`
const localConfig = ref({
  enabled: false,
  show_sidebar_nav: true,
  online_providers: [...defaultOnlineProviderIds],
  online_use_proxy: false,
  traditional_to_simplified: false,
  emby_refresh_after_subtitle_write: false,
  auto_search_on_transfer: false,
  auto_check_emby_subtitles: false,
  emby_library_servers: [],
  emby_query_libraries: null,
  emby_subtitle_path_mappings: "",
  trust_transfer_history_paths: false,
  manual_strm_enabled: true,
  manual_strm_paths: '',
  auto_search_on_manual_strm: true,
  timeline_max_offset_seconds: 120,
  timeline_min_offset_seconds: 0.2,
  timeline_vad_mode: 'webrtc',
  timeline_allow_risky_offset: false,
  subtitlecat_include_translations: true,
  ai_link_enabled: false,
  rar_dependency_mode: 'none',
  rar_tool_path: '/usr/bin/unar',
})

const embyScopeItems = [
  { title: '沿用 MoviePilot 已启用的 Emby', value: 'inherit' },
  { title: '仅查询所选媒体库', value: 'selected' },
]
const embyScopeEnabled = computed(() => localConfig.value.auto_check_emby_subtitles || localConfig.value.emby_refresh_after_subtitle_write)
const embyCustomScope = computed(() => embyScopeEnabled.value && localConfig.value.emby_query_libraries !== null)
const embyServers = ref([])
const embyLibrariesLoading = ref(false)
const embyServersLoaded = ref(false)
const embyLibrariesError = ref('')
const embyLoadedLibraries = ref([])
let lastEmbySelection = []

function embyLibraryKey(item) {
  return JSON.stringify([item.server, item.server_id || '', item.id])
}

const embyLibraryByKey = computed(() => new Map(
  [...(localConfig.value.emby_query_libraries || []), ...embyLoadedLibraries.value].map(item => [embyLibraryKey(item), item]),
))
const embyLibraryItems = computed(() => [...embyLibraryByKey.value]
  .sort(([, left], [, right]) => left.server.localeCompare(right.server))
  .map(([value, item]) => ({ value, title: `${item.server} · ${item.name}` })))
const embySelectedKeys = computed({
  get: () => (localConfig.value.emby_query_libraries || []).map(embyLibraryKey),
  set: keys => {
    localConfig.value.emby_query_libraries = (keys || []).map(key => embyLibraryByKey.value.get(key)).filter(Boolean)
  },
})

function setEmbyScope(mode) {
  if (mode === 'inherit') {
    lastEmbySelection = localConfig.value.emby_query_libraries || []
    localConfig.value.emby_query_libraries = null
  } else if (localConfig.value.emby_query_libraries === null) {
    localConfig.value.emby_query_libraries = lastEmbySelection
  }
}

async function readEmbyLibraries(servers = []) {
  if (embyLibrariesLoading.value) return
  embyLibrariesLoading.value = true
  embyLibrariesError.value = ''
  try {
    if (!props.api?.get) throw new Error('当前设置入口无法读取 MoviePilot 媒体服务器')
    const pluginApi = createJavSubtitleAssistantApi(props.api, () => `plugin/${props.pluginId}`)
    const read = async server => {
      const response = await pluginApi.embyQueryLibraries(server)
      if (response?.success === false) throw new Error(response.message || '读取 Emby 媒体库失败')
      return unwrapResponse(response)
    }
    if (!servers.length) {
      const result = await read()
      if (!Array.isArray(result?.servers)) throw new Error('Emby 服务器列表响应无效')
      embyServers.value = result.servers
      embyServersLoaded.value = true
      return
    }

    const errors = []
    await Promise.all(servers.map(async server => {
      try {
        const result = await read(server)
        if (!Array.isArray(result?.libraries)) throw new Error('Emby 媒体库列表响应无效')
        embyLoadedLibraries.value = [...embyLoadedLibraries.value.filter(item => item.server !== server), ...result.libraries]
        if (localConfig.value.emby_query_libraries !== null) {
          const refreshed = new Map(result.libraries.map(item => [embyLibraryKey(item), item]))
          localConfig.value.emby_query_libraries = localConfig.value.emby_query_libraries.map(item =>
            refreshed.get(embyLibraryKey(item)) || item,
          )
        }
      } catch (err) {
        errors.push(`${server}：${errorMessage(err, '读取 Emby 媒体库失败')}`)
      }
    }))
    embyLibrariesError.value = errors.join('；')
  } catch (err) {
    embyLibrariesError.value = errorMessage(err, '读取 Emby 媒体库失败')
  } finally {
    embyLibrariesLoading.value = false
  }
}

watch(embyCustomScope, enabled => {
  if (enabled && !embyServersLoaded.value) readEmbyLibraries()
})


const rarDependencyModes = [
  { title: '不处理，仅检测', value: 'none' },
  { title: '加载插件时尝试容器内安装 unar', value: 'container_install' },
  { title: '使用宿主机映射文件', value: 'mapped_binary' },
]

const timelineVadItems = [
  { title: 'WebRTC VAD（推荐）', value: 'webrtc' },
  { title: 'RMS 能量阈值（降级）', value: 'rms' },
]

function normalizeProviders(value) {
  const allowed = onlineProviderItems.map(item => item.value)
  const providers = Array.isArray(value) ? value.filter(item => allowed.includes(item)) : []
  return Array.isArray(value) && (!value.length || providers.length)
    ? Array.from(new Set(providers))
    : [...defaultOnlineProviderIds]
}

function normalizePositiveNumber(value, fallback) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function normalizeMultilinePaths(value) {
  const raw = Array.isArray(value) ? value : String(value || '').split(/\r?\n/)
  const paths = []
  raw.forEach(item => {
    const path = String(item || '').trim().replace(/[\\/]+$/, '')
    if (!path || paths.includes(path)) return
    paths.push(path)
  })
  return paths.join('\n')
}

function normalizeConfig(input) {
  const providers = normalizeProviders(input?.online_providers)
  const embyLibraries = input?.emby_query_libraries == null
    ? null
    : (Array.isArray(input.emby_query_libraries) ? input.emby_query_libraries : [])
  const embyServers = Array.isArray(input?.emby_library_servers)
    ? input.emby_library_servers
    : (embyLibraries || []).map(item => item?.server)
  return {
    enabled: Boolean(input?.enabled),
    show_sidebar_nav: input?.show_sidebar_nav !== false,
    online_providers: providers,
    online_use_proxy: Boolean(input?.online_use_proxy),
    online_proxy_migrated: true,
    traditional_to_simplified: Boolean(input?.traditional_to_simplified),
    emby_refresh_after_subtitle_write: Boolean(input?.emby_refresh_after_subtitle_write),
    auto_search_on_transfer: Boolean(input?.auto_search_on_transfer),
    auto_check_emby_subtitles: Boolean(input?.auto_check_emby_subtitles),
    emby_library_servers: [...new Set(embyServers.filter(name => typeof name === 'string' && name))],
    emby_query_libraries: embyLibraries,
    emby_subtitle_path_mappings: typeof input?.emby_subtitle_path_mappings === "string" ? input.emby_subtitle_path_mappings : "",
    trust_transfer_history_paths: Boolean(input?.trust_transfer_history_paths),
    manual_strm_enabled: Boolean(input?.manual_strm_enabled),
    manual_strm_paths: normalizeMultilinePaths(input?.manual_strm_paths),
    auto_search_on_manual_strm: Boolean(input?.auto_search_on_manual_strm),
    timeline_max_offset_seconds: Math.min(300, Math.max(1, Math.round(normalizePositiveNumber(input?.timeline_max_offset_seconds, 120)))),
    timeline_min_offset_seconds: normalizePositiveNumber(input?.timeline_min_offset_seconds, 0.2),
    timeline_vad_mode: timelineVadItems.some(item => item.value === input?.timeline_vad_mode) ? input.timeline_vad_mode : 'webrtc',
    timeline_allow_risky_offset: Boolean(input?.timeline_allow_risky_offset),
    subtitlecat_include_translations: input?.subtitlecat_include_translations !== false,
    ai_link_enabled: Boolean(input?.ai_link_enabled),
    rar_dependency_mode: ['none', 'container_install', 'mapped_binary'].includes(input?.rar_dependency_mode)
      ? input.rar_dependency_mode
      : 'none',
    rar_tool_path: String(input?.rar_tool_path || '/usr/bin/unar').trim() || '/usr/bin/unar',
  }
}

function saveConfig() {
  const normalized = normalizeConfig(localConfig.value)
  configError.value = ''
  emit('save', normalized)
}

onMounted(() => {
  localConfig.value = normalizeConfig(props.initialConfig)
})
</script>

<template>
  <div class="javsubtitleassistant-config">
    <div class="config-toolbar-sticky">
      <VToolbar density="comfortable" color="transparent">
        <div class="text-subtitle-1 ms-3 config-toolbar-title" title="JAV字幕助手配置">JAV字幕助手配置</div>
        <VSpacer />
        <VBtn
          href="https://github.com/felix-klk/MoviePilot-Plugins/tree/main/plugins.v3/javsubtitleassistant#使用"
          target="_blank"
          rel="noopener noreferrer"
          prepend-icon="mdi-book-open-page-variant"
          variant="text"
          color="primary"
          class="me-2"
        >
          使用教程
        </VBtn>
        <VBtn icon="mdi-content-save" variant="text" color="primary" :loading="props.saving" aria-label="保存配置" @click="saveConfig" />
        <VBtn icon="mdi-close" variant="text" :disabled="props.saving" aria-label="关闭配置" @click="emit('close')" />
      </VToolbar>
      <VDivider />
    </div>

    <div class="config-shell">
      <VCard rounded="xl" elevation="0" class="config-card">
        <VCardText>
          <VAlert
            v-if="configError"
            class="mb-4"
            type="error"
            variant="tonal"
            density="compact"
            :text="configError"
          />
          <div class="config-section">
            <div class="config-section-title">基础设置</div>
          </div>
          <div class="config-grid">
            <VSwitch
              v-model="localConfig.enabled"
              label="启用插件"
              color="primary"
              hide-details
            />
            <VSwitch
              v-model="localConfig.show_sidebar_nav"
              label="显示侧边栏入口"
              color="primary"
              hide-details
            />
            <VSwitch
              v-model="localConfig.ai_link_enabled"
              label="启用 AI 字幕联动"
              color="warning"
              hide-details
            />
            <VSwitch
              v-model="localConfig.traditional_to_simplified"
              label="写入前繁体转简体"
              color="success"
              hide-details
            />
            <VSwitch
              v-model="localConfig.emby_refresh_after_subtitle_write"
              label="写入后扫描 Emby 外挂字幕"
              color="info"
              hint="优先启动神医助手外挂字幕扫描任务；未找到任务时触发 Emby 全库刷新。"
              persistent-hint
            />
            <VSwitch
              v-model="localConfig.auto_search_on_transfer"
              label="入库后自动搜索匹配字幕"
              color="info"
              hide-details
            />
            <VSwitch
              v-model="localConfig.auto_check_emby_subtitles"
              label="自动下载前检查 Emby 字幕"
              color="info"
              hint="本地无法读取内嵌字幕时，查询 MoviePilot 已配置的 Emby。"
              persistent-hint
            />
            <VTextarea
              v-if="embyScopeEnabled"
              v-model="localConfig.emby_subtitle_path_mappings"
              label="Emby 路径映射"
              placeholder="/media => /mnt/media"
              hint="路径相同可留空。每行一组，左边填 MoviePilot 路径，右边填 Emby 路径。"
              persistent-hint
              rows="2"
              auto-grow
              variant="outlined"
              density="comfortable"
              class="config-grid-wide"
            />
            <VSelect
              v-if="embyScopeEnabled"
              :model-value="localConfig.emby_query_libraries === null ? 'inherit' : 'selected'"
              :items="embyScopeItems"
              label="Emby 查询范围"
              hint="范围用于检查字幕及写入后通知。实际扫描范围由 Emby 决定。"
              persistent-hint
              variant="outlined"
              density="comfortable"
              class="config-grid-wide"
              @update:model-value="setEmbyScope"
            />
            <template v-if="embyCustomScope">
              <VSelect
                v-model="localConfig.emby_library_servers"
                :items="embyServers"
                :loading="embyLibrariesLoading && !embyServersLoaded"
                label="Emby 服务器"
                variant="outlined"
                density="comfortable"
                multiple
                chips
                closable-chips
                hide-details
              />
              <VBtn
                v-if="!embyServersLoaded"
                :loading="embyLibrariesLoading"
                variant="tonal"
                @click="readEmbyLibraries()"
              >读取服务器列表</VBtn>
              <VBtn
                v-else
                :disabled="!localConfig.emby_library_servers.length"
                :loading="embyLibrariesLoading"
                variant="tonal"
                @click="readEmbyLibraries(localConfig.emby_library_servers)"
              >读取所选服务器的媒体库</VBtn>
              <VAlert v-if="embyLibrariesError" type="error" variant="tonal" class="config-grid-wide" :text="embyLibrariesError" />
              <VSelect
                v-model="embySelectedKeys"
                :items="embyLibraryItems"
                label="参与查询的媒体库"
                :hint="embySelectedKeys.length ? '未选媒体库的服务器会被跳过。' : '未选择媒体库，字幕检查仅使用本地信息。'"
                no-data-text="请先读取需要使用的服务器的媒体库"
                persistent-hint
                multiple
                chips
                closable-chips
                clearable
                variant="outlined"
                density="comfortable"
                class="config-grid-wide"
              />
            </template>
            <VSwitch
              v-model="localConfig.trust_transfer_history_paths"
              label="信任整理历史路径"
              color="warning"
              hint="CD2、网盘挂载、SMB 等慢路径可开启，刷新资源清单时不逐条访问文件。"
              persistent-hint
            />
            <VSwitch
              v-model="localConfig.manual_strm_enabled"
              label="监控本地整理目录"
              color="info"
              hint="开启后展开目录配置，并实时监控视频、STRM、NFO 和同目录字幕变化。"
              persistent-hint
            />
            <VTextarea
              v-if="localConfig.manual_strm_enabled"
              v-model="localConfig.manual_strm_paths"
              label="本地整理目录"
              :placeholder="manualStrmPathPlaceholder"
              rows="3"
              auto-grow
              hint="每行一个 MoviePilot 容器内可见的绝对路径；不读取 .strm 内容。"
              persistent-hint
              variant="outlined"
              density="comfortable"
              class="config-grid-wide"
            />
            <VSwitch
              v-if="localConfig.manual_strm_enabled"
              v-model="localConfig.auto_search_on_manual_strm"
              label="本地视频新增或更新时自动补中文字幕"
              color="success"
              hint="已有中文字幕就跳过，否则尝试补一份。"
              persistent-hint
            />
          </div>

          <VDivider class="my-5" />

          <div class="config-section">
            <div>
              <div class="config-section-title">在线字幕搜索</div>
              <p>在线字幕源为 SubtitleCat。Subtitle Cat 请求至少间隔 3 秒，字幕下载串行且每份结束后等待至少 8 秒。</p>
            </div>
          </div>

          <div class="online-source-settings">
            <div class="config-grid two-column online-source-general">
              <VSelect
                v-model="localConfig.online_providers"
                :items="onlineProviderItems"
                label="启用字幕源"
                variant="outlined"
                density="comfortable"
                multiple
                chips
                hide-details
              />
              <VSelect
                v-model="localConfig.online_use_proxy"
                :items="[
                  { title: '不使用系统代理', value: false },
                  { title: '使用 MoviePilot 系统代理', value: true },
                ]"
                label="API 请求代理"
                variant="outlined"
                density="comfortable"
                hide-details
              />
            </div>
            <section class="provider-settings" aria-labelledby="provider-subtitlecat-title">
              <div class="provider-heading">
                <h3 id="provider-subtitlecat-title">SubtitleCat</h3>
                <p>无需 API Key</p>
              </div>
              <div class="provider-body">
                <div class="config-grid">
                  <VSwitch
                    v-model="localConfig.subtitlecat_include_translations"
                    label="接受已有中文站点翻译"
                    hint="开启后也匹配网站已有的中文字幕译文，不发起翻译任务。"
                    persistent-hint
                    color="primary"
                  />
                </div>
              </div>
            </section>
          </div>

          <VDivider class="my-5" />

          <div class="config-section">
            <div>
              <div class="config-section-title">智能调轴</div>
              <p>控制写入前可接受的全局偏移范围；超过 120 秒通常意味着错集、错版本或整季包映射错误。</p>
            </div>
          </div>

          <div class="config-grid two-column">
            <VTextField
              v-model.number="localConfig.timeline_max_offset_seconds"
              label="智能调轴最大偏移秒数"
              type="number"
              min="1"
              max="300"
              suffix="秒"
              hint="默认 120；不建议超过 120 秒。超过 120 秒的手动操作会再次确认，自动入库不放行高风险结果。"
              persistent-hint
              variant="outlined"
              density="comfortable"
            />
            <VTextField
              v-model.number="localConfig.timeline_min_offset_seconds"
              label="最小应用阈值"
              type="number"
              min="0.1"
              step="0.1"
              suffix="秒"
              hint="低于该阈值时仅复制字幕，不做时间轴改写。"
              persistent-hint
              variant="outlined"
              density="comfortable"
            />
            <VSelect
              v-model="localConfig.timeline_vad_mode"
              :items="timelineVadItems"
              label="音频 VAD 模式"
              variant="outlined"
              density="comfortable"
              hide-details
            />
            <VSwitch
              v-model="localConfig.timeline_allow_risky_offset"
              label="全局允许高风险偏移"
              color="warning"
              hint="不建议开启。开启后后端会允许 120 秒以上结果；手动操作仍会显示风险确认。"
              persistent-hint
            />
          </div>

          <VDivider class="my-5" />

          <div class="config-section">
            <div>
              <div class="config-section-title">RAR / 7Z 解压器</div>
              <p>RAR / 7Z 默认使用 MoviePilot 容器内的 <code>/usr/bin/unar</code>。</p>
            </div>
          </div>

          <div class="config-grid">
            <VSelect
              v-model="localConfig.rar_dependency_mode"
              :items="rarDependencyModes"
              label="压缩包解压器处理方式"
              variant="outlined"
              density="comfortable"
              hide-details
            />
            <VTextField
              v-model="localConfig.rar_tool_path"
              label="容器内映射路径"
              placeholder="/usr/bin/unar"
              variant="outlined"
              density="comfortable"
              hide-details
            />
          </div>
          <VAlert
            class="mt-4"
            type="info"
            variant="tonal"
            text="当前 MoviePilot 容器内置 unar 时无需额外安装；如使用映射文件，请把 unar 映射到容器内 /usr/bin/unar。插件不会主动重启 Docker 容器。"
          />
        </VCardText>
      </VCard>
    </div>
  </div>
</template>

<style scoped>
.config-toolbar-sticky {
  position: sticky;
  top: 0;
  z-index: 4;
  background: rgb(var(--v-theme-surface));
  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.08);
}

.config-shell {
  padding: 20px;
}

.config-card {
  border: 1px solid var(--smu-border);
  background: var(--smu-card-bg-strong);
  backdrop-filter: blur(12px);
  box-shadow: var(--smu-shadow);
}

.config-grid {
  display: grid;
  gap: 16px;
}

.online-source-settings {
  container-type: inline-size;
}

.online-source-general {
  margin-bottom: 8px;
}

.provider-settings {
  display: grid;
  grid-template-columns: 184px minmax(0, 1fr);
  gap: 24px;
  padding: 24px 0;
  border-top: 1px solid var(--smu-border);
}

.provider-settings:last-child {
  padding-bottom: 0;
}

.provider-heading h3 {
  margin: 0;
  color: var(--smu-text);
  font-size: 15px;
  font-weight: 700;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.provider-heading p {
  margin: 6px 0 0;
  color: var(--smu-text-muted);
  font-size: 13px;
  line-height: 1.6;
  overflow-wrap: anywhere;
}

.provider-body {
  display: grid;
  align-content: start;
  gap: 16px;
  min-width: 0;
}

.provider-body .config-grid {
  align-items: start;
}

@container (max-width: 720px) {
  .provider-settings {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
  }

  .online-source-settings .config-grid.two-column {
    grid-template-columns: minmax(0, 1fr);
  }
}

.config-grid.two-column {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.config-section {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.config-section-title {
  color: var(--smu-text);
  font-size: 15px;
  font-weight: 800;
  letter-spacing: 0.04em;
}

.config-section p {
  margin: 4px 0 0;
  color: var(--smu-text-muted);
  font-size: 12px;
}

.config-toolbar-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.config-switch-line {
  min-height: 48px;
}

.dialog-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

@media (max-width: 760px) {
  .config-grid.two-column {
    grid-template-columns: 1fr;
  }

  .config-section {
    align-items: stretch;
    flex-direction: column;
  }

}
</style>
