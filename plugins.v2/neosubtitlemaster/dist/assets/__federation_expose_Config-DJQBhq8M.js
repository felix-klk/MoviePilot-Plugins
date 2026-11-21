import { importShared } from './__federation_fn_import-JrT3xvdd.js';

function unwrapResponse(response) {
  if (response && Object.prototype.hasOwnProperty.call(response, 'data') && response.success !== undefined) {
    return response.data
  }
  return response?.data ?? response
}

function mediaLabel(media) {
  if (!media) return ''
  return media.year ? `${media.title} (${media.year})` : `${media.title || ''}`
}

function targetLabel(target) {
  return target?.label || target?.target_label || ''
}

function formatMediaType(type) {
  return type === 'tv' ? '剧集' : '电影'
}

function rarDependencyModeLabel(mode) {
  if (mode === 'container_install') return '容器内自动安装'
  if (mode === 'mapped_binary') return '宿主机映射文件'
  return '仅检测'
}

function seasonLabel(season) {
  const value = Number(season || 0);
  return value === 0 ? '特别篇' : `第 ${value} 季`
}

function compactTargetName(target) {
  if (!target) return ''
  if (target.media_type !== 'tv') return target.basename || targetLabel(target)
  const season = Number(target.season || 0);
  const episode = Number(target.episode || 0);
  if (season && episode) {
    return `S${String(season).padStart(2, '0')}E${String(episode).padStart(2, '0')} · ${target.basename || targetLabel(target)}`
  }
  return target.basename || targetLabel(target)
}

function mediaStat(media) {
  const count = Number(media?.local_count || 0);
  if (media?.media_type === 'tv') {
    const seasonCount = Number(media?.season_count || 0);
    return `${seasonCount || '-'} 季 · ${count} 集本地资源`
  }
  return `${count || 1} 个本地资源`
}

function historyMediaStat(item) {
  const subtitleCount = Number(item?.subtitle_count || 0);
  const targetCount = Number(item?.target_count || 0);
  if (item?.media_type === 'tv') return `${targetCount} 集 · ${subtitleCount} 个外挂字幕`
  return `${subtitleCount} 个外挂字幕`
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${Math.max(1, Math.round(size / 1024))} KB`
  return `${size} B`
}

function formatOffset(value) {
  const number = Number(value || 0);
  return `${number >= 0 ? '+' : ''}${number.toFixed(3)}s`
}

function timelineBaseText(base) {
  const value = String(base || '');
  if (value.startsWith('embedded:')) return '内嵌字幕基准'
  if (value === 'audio:rms' || value === 'audio:rms:cache') return 'RMS 音频检测（低精度）'
  if (value === 'audio:webrtc' || value === 'audio:webrtc:cache') return 'WebRTC 音频检测'
  if (value.startsWith('audio:')) return '音频基准'
  return value || '未知基准'
}

function timelineConfidenceText(value) {
  const known = {
    high: '高可信',
    medium: '中可信',
    low: '低可信',
    rejected: '已拒绝',
  };
  return known[value] || value || '未知可信度'
}

function timelineRiskText(value) {
  const known = {
    offset_over_120s: '偏移超过 120s',
    offset_over_configured_max: '超过配置最大偏移',
    low_score: '匹配分数过低',
    weak_score_margin: '最佳与次优差距过小',
    unstable_subtitle_activity: '字幕活动区间异常',
    unusual_scale_factor: '帧率比例异常',
  };
  return known[value] || value
}

function timelineResultText(item) {
  const timeline = item?.timeline || {};
  if (!timeline.enabled) return '未启用智能调轴'
  const base = timelineBaseText(timeline.base);
  if (timeline.applied) {
    return `已调轴 ${formatOffset(timeline.offset_seconds)} · ${base}`
  }
  return `未调整：偏移 ${formatOffset(timeline.offset_seconds)} 小于阈值 · ${base}`
}

function timelineMetaItems(item) {
  const timeline = item?.timeline || item || {};
  if (!timeline.enabled) return []
  const items = [];
  if (timeline.confidence) items.push(`置信度：${timelineConfidenceText(timeline.confidence)}`);
  if (timeline.score_margin !== undefined) items.push(`差距：${Number(timeline.score_margin || 0).toFixed(3)}`);
  if (timeline.active_ratio !== undefined) items.push(`活动：${(Number(timeline.active_ratio || 0) * 100).toFixed(1)}%`)
  ;(timeline.risk_flags || []).forEach(flag => items.push(timelineRiskText(flag)));
  return items
}

function readableErrorDetail(value) {
  if (!value) return ''
  if (typeof value === 'string') return value
  if (Array.isArray(value)) {
    return value
      .map(item => readableErrorDetail(item))
      .filter(Boolean)
      .join('；')
  }
  if (typeof value === 'object') {
    const direct = value.message || value.msg || value.detail || value.reason || value.error;
    if (direct) return readableErrorDetail(direct)
    const parts = [];
    if (Array.isArray(value.loc) && value.loc.length) parts.push(value.loc.join('.'));
    if (value.type) parts.push(value.type);
    if (parts.length) return parts.join('：')
    try {
      return JSON.stringify(value, null, 0)
    } catch (_) {
      return String(value)
    }
  }
  return String(value)
}

function errorMessage(err, fallback) {
  return readableErrorDetail(
    err?.response?.data?.detail
    || err?.response?.data?.message
    || err?.data?.detail
    || err?.data?.message
    || err?.message
    || fallback
  )
}

const languageAliases = new Map(Object.entries({
  zh: 'chi', 'zh-hans': 'chi', zh_hans: 'chi', 'zh-cn': 'chi', zh_cn: 'chi',
  'zh-hant': 'cht', zh_hant: 'cht', 'zh-tw': 'cht', zh_tw: 'cht',
  chs: 'chi', tw: 'cht', hk: 'cht', zho: 'chi', cmn: 'chi', cn: 'chi',
  中英双语: 'chi&eng', 繁英双语: 'cht&eng',
  en: 'eng', ja: 'jpn', jp: 'jpn', ko: 'kor', kr: 'kor',
  fr: 'fre', fra: 'fre', de: 'ger', deu: 'ger', es: 'spa', pt: 'por', it: 'ita', ru: 'rus',
}));

const embyFilenameTags = new Map(Object.entries({
  chi: 'zh-CN', cht: 'zh-TW', 'chi&eng': '中英双语.zh-CN', 'cht&eng': '繁英双语.zh-TW',
}));

function buildOutputName(target, item) {
  if (!target) return ''
  const basename = target.basename || 'subtitle';
  const raw = String(item?.language_suffix || '').trim().toLowerCase();
  const normalized = languageAliases.get(raw) || raw;
  const isPair = /[&+/,]/.test(normalized);
  const parts = normalized.split(/[&+/,]+/).map(part => {
    const code = languageAliases.get(part.trim()) || part.trim();
    const pairCode = isPair ? ({ jpn: 'jp', kor: 'kr' }[code] || code) : code;
    return pairCode.replace(/[^a-z0-9-]/g, '')
  }).filter(Boolean);
  const languages = [...new Set(parts)];
  const suffix = embyFilenameTags.get([...languages].sort().join('&')) || languages.join('&') || 'und';
  const flag = item?.default_subtitle === true ? '.default' : '';
  let ext = item?.ext || '.srt';
  if (!ext.startsWith('.')) ext = `.${ext}`;
  return `${basename}.${suffix}${flag}${ext.toLowerCase()}`
}

function resolvePluginBase(pluginBase) {
  const raw = typeof pluginBase === 'function' ? pluginBase() : (pluginBase?.value ?? pluginBase);
  return raw || 'plugin/NeoSubtitleMaster'
}

function createNeoSubtitleMasterApi(api, pluginBase) {
  const get = endpoint => api.get(`${resolvePluginBase(pluginBase)}${endpoint}`);
  const post = (endpoint, payload) => api.post(`${resolvePluginBase(pluginBase)}${endpoint}`, payload);

  return {
    unwrapResponse,
    clearSubtitles(payload) {
      return post('/clear_subtitles', payload)
    },
    timelineFixExisting(payload) {
      return post('/timeline_fix_existing', payload)
    },
    restoreSubtitleBackup(payload) {
      return post('/restore_subtitle_backup', payload)
    },
    aiTasks(payload) {
      return post('/ai_tasks', payload)
    },
    timelineTasks(payload) {
      return post('/timeline_tasks', payload)
    },
    aiSubmit(payload) {
      return post('/ai_submit', payload)
    },
    aiCancel(payload) {
      return post('/ai_cancel', payload)
    },
    aiRestart(payload) {
      return post('/ai_restart', payload)
    },
    status() {
      return get('/status')
    },
    embyQueryLibraries(server = '') {
      const query = server ? '?' + new URLSearchParams({ server }).toString() : '';
      return get('/emby_query_libraries' + query)
    },
    autoTransferQueue() {
      return get('/auto_transfer_queue')
    },
    retryAutoTransferTask(payload) {
      return post('/auto_transfer_queue/retry', payload)
    },
    enqueueBilingualFollowup(payload) {
      return post('/auto_transfer_queue/followup', payload)
    },
    cancelBilingualFollowup(payload) {
      return post('/auto_transfer_queue/cancel_followup', payload)
    },
    clearAutoTransferHistory(payload = {}) {
      return post('/auto_transfer_queue/clear_history', payload)
    },
    onlineStatus() {
      return get('/online_status')
    },
    scanLocalDirectories() {
      return post('/local_directories/scan', {})
    },
    refreshIndex(payload = {}) {
      return post('/refresh_index', payload)
    },
    search(params) {
      return get(`/search?${params.toString()}`)
    },
    matchHistory(params) {
      return get(`/match_history?${params.toString()}`)
    },
    targets(params) {
      return get(`/targets?${params.toString()}`)
    },
    deleteSubtitle(payload) {
      return post('/delete_subtitle', payload)
    },
    onlineManualLinks(payload) {
      return post('/online_manual_links', payload)
    },
    onlineSearchProvider(payload) {
      return post('/online_search_provider', payload)
    },
    onlineAutoPreview(payload) {
      return post('/online_auto_preview', payload)
    },
    onlineAiSubmit(payload) {
      return post('/online_ai_submit', payload)
    },
    onlineDownloadPreview(payload) {
      return post('/online_download_preview', payload)
    },
    prepareUpload(formData) {
      return post('/prepare_upload', formData)
    },
    applyUpload(payload) {
      return post('/apply_upload', payload)
    },
  }
}

const providerCatalog = [
	{
		id: "subhd",
		name: "SubHD",
		root_url: "https://subhd.tv",
		default_enabled: true,
		priority: 35,
		media_types: [
			"movie",
			"tv"
		]
	},
	{
		id: "assrt",
		name: "射手网(伪)",
		root_url: "https://2.assrt.net",
		default_enabled: false,
		priority: 30,
		media_types: [
			"movie",
			"tv"
		]
	},
	{
		id: "opensubtitles",
		name: "OpenSubtitles",
		root_url: "https://www.opensubtitles.com",
		default_enabled: false,
		priority: 20,
		media_types: [
			"movie",
			"tv"
		]
	},
	{
		id: "subtitlecat",
		name: "SubtitleCat",
		root_url: "https://www.subtitlecat.com",
		default_enabled: true,
		priority: 10,
		media_types: [
			"movie",
			"tv"
		]
	},
	{
		id: "subdl",
		name: "SubDL",
		root_url: "https://subdl.com",
		default_enabled: false,
		priority: 20,
		media_types: [
			"movie",
			"tv"
		]
	}
];

const onlineProviderItems = providerCatalog.map(item => ({ title: item.name, value: item.id }));
const defaultOnlineProviderIds = providerCatalog.filter(item => item.default_enabled).map(item => item.id);

const onlineLanguageItems = [
  { title: '中英', value: 'chi&eng' },
  { title: '中日', value: 'chi&jp' },
  { title: '中韩', value: 'chi&kr' },
  { title: '其他双语', value: 'other_bilingual' },
  { title: '中文', value: 'chinese' },
  { title: '英文', value: 'english' },
  { title: '日文', value: 'japanese' },
  { title: '韩文', value: 'korean' },
  { title: '其他', value: 'other' },
  { title: '待确认', value: 'und' },
];

function onlineResultKey(item) {
  return `${item?.provider || 'unknown'}:${item?.result_id || item?.page_url || item?.title || ''}`
}

function providerName(providerId) {
  const known = onlineProviderItems.find(item => item.value === providerId);
  return known?.title || providerId || '未知来源'
}

function providerPriority(providerId) {
  return providerCatalog.find(item => item.id === providerId)?.priority || 0
}

function onlineResultMeta(item) {
  const parts = [];
  if (item.language) parts.push(item.language);
  if (item.translation_source === 'site_translation') parts.push('站点翻译');
  if (item.translation_source === 'original_upload') parts.push('原始上传');
  if (item.format) parts.push(item.format);
  if (item.season || item.episode) {
    parts.push(`S${String(item.season || 0).padStart(2, '0')}E${String(item.episode || 0).padStart(2, '0')}`);
  }
  if (item.score) parts.push(`匹配 ${item.score}`);
  return parts.join(' · ') || '等待下载后自动匹配'
}

function isOnlineResultDownloadable(item) {
  return item?.downloadable !== false
}

function onlineResultLanguageCategory(item) {
  const category = String(item?.language_category || '').toLowerCase();
  if (['chinese', 'english', 'japanese', 'korean', 'other'].includes(category)) return category
  const filter = onlineResultLanguageFilterCategory(item);
  if (['chi&eng', 'chi&jp', 'chi&kr', 'other_bilingual'].includes(filter)) return 'chinese'
  return filter === 'und' ? 'other' : filter
}

function onlineResultLanguageFilterCategory(item) {
  const bucket = item?.language_bucket || 'und';
  const category = { chi: 'chinese', cht: 'chinese', eng: 'english', jpn: 'japanese', kor: 'korean' }[bucket] || bucket;
  return onlineLanguageItems.some(item => item.value === category) ? category : 'und'
}

function onlineResultLanguagePriority(item) {
  const category = onlineResultLanguageCategory(item);
  if (category === 'chinese') return 40
  if (category === 'english') return 30
  if (category === 'japanese' || category === 'korean') return 20
  return 10
}

function onlineResultIdentityPriority(item) {
  const status = String(item?.identity_status || '').toLowerCase();
  if (status === 'strong') return 30
  if (status === 'weak') return 10
  return 0
}

function isForeignOnlineResult(item) {
  return onlineResultLanguageCategory(item) !== 'chinese'
}

function providerProgressText(state) {
  if (state === 'searching') return '搜索中'
  if (state === 'done') return '已完成'
  if (state === 'timeout') return '超时'
  if (state === 'cancelled') return '已停止'
  if (state === 'error') return '失败'
  return '等待'
}

function providerProgressColor(state) {
  if (state === 'searching') return 'info'
  if (state === 'done') return 'success'
  if (state === 'timeout') return 'warning'
  if (state === 'cancelled') return 'default'
  if (state === 'error') return 'warning'
  return 'default'
}

const _export_sfc = (sfc, props) => {
  const target = sfc.__vccOpts || sfc;
  for (const [key, val] of props) {
    target[key] = val;
  }
  return target;
};

const {createElementVNode:_createElementVNode,resolveComponent:_resolveComponent,createVNode:_createVNode,createTextVNode:_createTextVNode,withCtx:_withCtx,openBlock:_openBlock,createBlock:_createBlock,createCommentVNode:_createCommentVNode,Fragment:_Fragment,createElementBlock:_createElementBlock,toDisplayString:_toDisplayString,renderList:_renderList,withModifiers:_withModifiers,normalizeClass:_normalizeClass,unref:_unref} = await importShared('vue');


const _hoisted_1 = { class: "neosubtitlemaster-config" };
const _hoisted_2 = { class: "config-toolbar-sticky" };
const _hoisted_3 = { class: "config-shell" };
const _hoisted_4 = { class: "config-grid" };
const _hoisted_5 = {
  key: 5,
  class: "config-grid-wide"
};
const _hoisted_6 = {
  key: 6,
  class: "language-priority-details"
};
const _hoisted_7 = { class: "language-priority-summary" };
const _hoisted_8 = { class: "language-priority-summary-hint" };
const _hoisted_9 = { class: "language-priority-order" };
const _hoisted_10 = ["onDragover", "onDrop"];
const _hoisted_11 = ["aria-label", "onDragstart"];
const _hoisted_12 = { class: "language-priority-name" };
const _hoisted_13 = ["value", "aria-label", "max", "onChange"];
const _hoisted_14 = { class: "online-source-settings" };
const _hoisted_15 = { class: "config-grid two-column online-source-general" };
const _hoisted_16 = {
  class: "provider-settings",
  "aria-labelledby": "provider-subhd-title"
};
const _hoisted_17 = { class: "provider-body" };
const _hoisted_18 = { class: "config-grid" };
const _hoisted_19 = {
  class: "provider-settings",
  "aria-labelledby": "provider-subtitlecat-title"
};
const _hoisted_20 = { class: "provider-body" };
const _hoisted_21 = { class: "config-grid" };
const _hoisted_22 = {
  class: "provider-settings",
  "aria-labelledby": "provider-subdl-title"
};
const _hoisted_23 = { class: "provider-body" };
const _hoisted_24 = { class: "config-grid" };
const _hoisted_25 = {
  class: "provider-settings",
  "aria-labelledby": "provider-assrt-title"
};
const _hoisted_26 = { class: "provider-body" };
const _hoisted_27 = { class: "config-grid" };
const _hoisted_28 = { class: "config-grid two-column" };
const _hoisted_29 = {
  class: "provider-settings",
  "aria-labelledby": "provider-opensubtitles-title"
};
const _hoisted_30 = { class: "provider-body" };
const _hoisted_31 = { class: "config-grid" };
const _hoisted_32 = { class: "config-grid two-column" };
const _hoisted_33 = { class: "config-grid two-column" };
const _hoisted_34 = { class: "config-grid two-column" };
const _hoisted_35 = { class: "config-grid" };

const {computed,onMounted,ref,watch} = await importShared('vue');

const manualStrmPathPlaceholder = `E:\\test
/media/test`;

const _sfc_main = {
  __name: 'Config',
  props: {
  api: { type: Object, default: null },
  pluginId: { type: String, default: 'NeoSubtitleMaster' },
  initialConfig: {
    type: Object,
    default: () => ({}),
  },
  saving: { type: Boolean, default: false },
},
  emits: ['save', 'close'],
  setup(__props, { emit: __emit }) {

const props = __props;

const emit = __emit;
const configError = ref('');
const draggingAutoLanguage = ref('');
const dropTargetAutoLanguage = ref('');
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
  auto_skip_chinese_media_on_transfer: true,
  auto_transfer_subtitle_strategy: 'online_then_ai_source',
  trust_transfer_history_paths: false,
  manual_strm_enabled: false,
  manual_strm_paths: '',
  auto_search_on_manual_strm: false,
  auto_multi_subtitle_mode: 'best',
  auto_subtitle_language_priority: ['chi&eng', 'chi&jp', 'chi&kr', 'other_bilingual', 'chi', 'cht', 'eng'],
  auto_subtitle_language_strict: false,
  auto_subtitle_format_priority: ['.ass', '.srt', '.ssa', '.vtt'],
  auto_ass_to_srt_for_ai: true,
  timeline_max_offset_seconds: 120,
  timeline_min_offset_seconds: 0.2,
  timeline_vad_mode: 'webrtc',
  timeline_allow_risky_offset: false,
  subhd_url: 'https://subhd.tv',
  subdl_api_key: '',
  subhd_bypass_url: '',
  subtitlecat_include_translations: false,
  assrt_url: 'https://2.assrt.net',
  assrt_api_key: '',
  assrt_api_url: 'https://api.assrt.net',
  opensubtitles_url: 'https://www.opensubtitles.com',
  opensubtitles_api_key: '',
  opensubtitles_api_url: 'https://api.opensubtitles.com/api/v1',
  opensubtitles_username: '',
  opensubtitles_password: '',
  ai_link_enabled: true,
  rar_dependency_mode: 'none',
  rar_tool_path: '/usr/bin/unar',
});

const embyScopeItems = [
  { title: '沿用 MoviePilot 已启用的 Emby', value: 'inherit' },
  { title: '仅查询所选媒体库', value: 'selected' },
];
const embyScopeEnabled = computed(() => localConfig.value.auto_check_emby_subtitles || localConfig.value.emby_refresh_after_subtitle_write);
const embyCustomScope = computed(() => embyScopeEnabled.value && localConfig.value.emby_query_libraries !== null);
const directoryScanLoading = ref(false);
const directoryScanNotice = ref('');
const directoryScanError = ref('');
const embyServers = ref([]);
const embyLibrariesLoading = ref(false);
const embyServersLoaded = ref(false);
const embyLibrariesError = ref('');
const embyLoadedLibraries = ref([]);
let lastEmbySelection = [];

function embyLibraryKey(item) {
  return JSON.stringify([item.server, item.server_id || '', item.id])
}

const embyLibraryByKey = computed(() => new Map(
  [...(localConfig.value.emby_query_libraries || []), ...embyLoadedLibraries.value].map(item => [embyLibraryKey(item), item]),
));
const embyLibraryItems = computed(() => [...embyLibraryByKey.value]
  .sort(([, left], [, right]) => left.server.localeCompare(right.server))
  .map(([value, item]) => ({ value, title: `${item.server} · ${item.name}` })));
const embySelectedKeys = computed({
  get: () => (localConfig.value.emby_query_libraries || []).map(embyLibraryKey),
  set: keys => {
    localConfig.value.emby_query_libraries = (keys || []).map(key => embyLibraryByKey.value.get(key)).filter(Boolean);
  },
});

function setEmbyScope(mode) {
  if (mode === 'inherit') {
    lastEmbySelection = localConfig.value.emby_query_libraries || [];
    localConfig.value.emby_query_libraries = null;
  } else if (localConfig.value.emby_query_libraries === null) {
    localConfig.value.emby_query_libraries = lastEmbySelection;
  }
}

async function readEmbyLibraries(servers = []) {
  if (embyLibrariesLoading.value) return
  embyLibrariesLoading.value = true;
  embyLibrariesError.value = '';
  try {
    if (!props.api?.get) throw new Error('当前设置入口无法读取 MoviePilot 媒体服务器')
    const pluginApi = createNeoSubtitleMasterApi(props.api, () => `plugin/${props.pluginId}`);
    const read = async server => {
      const response = await pluginApi.embyQueryLibraries(server);
      if (response?.success === false) throw new Error(response.message || '读取 Emby 媒体库失败')
      return unwrapResponse(response)
    };
    if (!servers.length) {
      const result = await read();
      if (!Array.isArray(result?.servers)) throw new Error('Emby 服务器列表响应无效')
      embyServers.value = result.servers;
      embyServersLoaded.value = true;
      return
    }

    const errors = [];
    await Promise.all(servers.map(async server => {
      try {
        const result = await read(server);
        if (!Array.isArray(result?.libraries)) throw new Error('Emby 媒体库列表响应无效')
        embyLoadedLibraries.value = [...embyLoadedLibraries.value.filter(item => item.server !== server), ...result.libraries];
        if (localConfig.value.emby_query_libraries !== null) {
          const refreshed = new Map(result.libraries.map(item => [embyLibraryKey(item), item]));
          localConfig.value.emby_query_libraries = localConfig.value.emby_query_libraries.map(item =>
            refreshed.get(embyLibraryKey(item)) || item,
          );
        }
      } catch (err) {
        errors.push(`${server}：${errorMessage(err, '读取 Emby 媒体库失败')}`);
      }
    }));
    embyLibrariesError.value = errors.join('；');
  } catch (err) {
    embyLibrariesError.value = errorMessage(err, '读取 Emby 媒体库失败');
  } finally {
    embyLibrariesLoading.value = false;
  }
}

watch(embyCustomScope, enabled => {
  if (enabled && !embyServersLoaded.value) readEmbyLibraries();
});


const rarDependencyModes = [
  { title: '不处理，仅检测', value: 'none' },
  { title: '加载插件时尝试容器内安装 unar', value: 'container_install' },
  { title: '使用宿主机映射文件', value: 'mapped_binary' },
];

const autoStrategyItems = [
  { title: '在线匹配优先，AI 来源兜底', value: 'online_then_ai_source' },
  { title: '只用在线匹配来源', value: 'online_source_only' },
  { title: '只用 AI 来源生成', value: 'ai_source_only' },
];

const autoMultiSubtitleModes = [
  { title: '按偏好选择最佳', value: 'best' },
  { title: '中文/双语全部入库', value: 'chinese_all' },
  { title: '全部入库', value: 'all' },
];

const autoLanguageItems = [
  { title: '中英双语', value: 'chi&eng' },
  { title: '中日双语', value: 'chi&jp' },
  { title: '中韩双语', value: 'chi&kr' },
  { title: '其他中外双语', value: 'other_bilingual' },
  { title: '简中', value: 'chi' },
  { title: '繁中', value: 'cht' },
  { title: '英文', value: 'eng' },
  { title: '日文', value: 'jpn' },
  { title: '韩文', value: 'kor' },
];
const defaultAutoLanguages = ['chi&eng', 'chi&jp', 'chi&kr', 'other_bilingual', 'chi', 'cht', 'eng'];

const autoFormatItems = [
  { title: 'ASS', value: '.ass' },
  { title: 'SRT', value: '.srt' },
  { title: 'SSA', value: '.ssa' },
  { title: 'VTT', value: '.vtt' },
  { title: 'WebVTT', value: '.webvtt' },
  { title: 'SBV', value: '.sbv' },
  { title: 'SUB', value: '.sub' },
  { title: 'SUP', value: '.sup' },
  { title: 'VobSub (IDX)', value: '.idx' },
  { title: 'SMI', value: '.smi' },
  { title: 'SAMI', value: '.sami' },
  { title: 'TTML', value: '.ttml' },
  { title: 'DFXP', value: '.dfxp' },
];

const timelineVadItems = [
  { title: 'WebRTC VAD（推荐）', value: 'webrtc' },
  { title: 'RMS 能量阈值（降级）', value: 'rms' },
];

function normalizeProviders(value) {
  const allowed = onlineProviderItems.map(item => item.value);
  const providers = Array.isArray(value) ? value.filter(item => allowed.includes(item)) : [];
  return Array.isArray(value) ? Array.from(new Set(providers)) : [...defaultOnlineProviderIds]
}

function normalizeRootUrl(value, fallback) {
  const text = String(value || '').trim().replace(/\/+$/, '');
  return /^https?:\/\//i.test(text) ? text : fallback
}


function normalizePositiveNumber(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback
}

function normalizeAutoStrategy(value) {
  const aliases = {
    search_first: 'online_then_ai_source',
    search_only: 'online_source_only',
    ai_only: 'ai_source_only',
    ai_first: 'ai_source_only',
  };
  const normalized = aliases[value] || value;
  return autoStrategyItems.some(item => item.value === normalized)
    ? normalized
    : 'online_then_ai_source'
}

function normalizeList(value, allowed, fallback) {
  const raw = Array.isArray(value) ? value : String(value || '').split(/[\s,，/|]+/);
  const result = [];
  raw.forEach(item => {
    const normalized = String(item || '').trim().toLowerCase();
    if (allowed.includes(normalized) && !result.includes(normalized)) {
      result.push(normalized);
    }
  });
  fallback.forEach(item => {
    if (!result.includes(item)) result.push(item);
  });
  return result
}

function normalizeLanguages(value) {
  if (value == null) return [...defaultAutoLanguages]
  const allowed = autoLanguageItems.map(item => item.value);
  const raw = Array.isArray(value) ? value : String(value).split(/[\s,，/|]+/);
  const result = [];
  raw.forEach(item => {
    const key = String(item || '').trim().toLowerCase();
    const expanded = ['bilingual', '双语'].includes(key) ? defaultAutoLanguages.slice(0, 4) : [key === 'cht&eng' ? 'chi&eng' : key];
    expanded.forEach(language => {
      if (allowed.includes(language) && !result.includes(language)) result.push(language);
    });
  });
  return result
}

function autoLanguageTitle(value) {
  return autoLanguageItems.find(item => item.value === value)?.title || value
}

function setAutoLanguageRank(language, value) {
  const priority = [...localConfig.value.auto_subtitle_language_priority];
  const currentIndex = priority.indexOf(language);
  const rank = Number(value);
  if (!Number.isInteger(rank) || rank < 1 || rank > priority.length || currentIndex < 0) return

  priority.splice(currentIndex, 1);
  priority.splice(rank - 1, 0, language);
  localConfig.value.auto_subtitle_language_priority = priority;
}

function moveAutoLanguage(source, target) {
  const priority = [...localConfig.value.auto_subtitle_language_priority];
  const sourceIndex = priority.indexOf(source);
  const targetIndex = priority.indexOf(target);
  if (sourceIndex < 0 || targetIndex < 0 || sourceIndex === targetIndex) return

  const [language] = priority.splice(sourceIndex, 1);
  priority.splice(targetIndex, 0, language);
  localConfig.value.auto_subtitle_language_priority = priority;
}

function startDraggingAutoLanguage(language, event) {
  draggingAutoLanguage.value = language;
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', language);
}

function finishDraggingAutoLanguage() {
  draggingAutoLanguage.value = '';
  dropTargetAutoLanguage.value = '';
}

function dropAutoLanguage(language, event) {
  event.preventDefault();
  moveAutoLanguage(draggingAutoLanguage.value || event.dataTransfer.getData('text/plain'), language);
  finishDraggingAutoLanguage();
}

function normalizeMultilinePaths(value) {
  const raw = Array.isArray(value) ? value : String(value || '').split(/\r?\n/);
  const paths = [];
  raw.forEach(item => {
    const path = String(item || '').trim().replace(/[\\/]+$/, '');
    if (!path || paths.includes(path)) return
    paths.push(path);
  });
  return paths.join('\n')
}

function normalizeConfig(input) {
  const assrtApiKey = String(input?.assrt_api_key || '').trim();
  const opensubtitlesApiKey = String(input?.opensubtitles_api_key || '').trim();
  const opensubtitlesUsername = String(input?.opensubtitles_username || '').trim();
  const opensubtitlesPassword = String(input?.opensubtitles_password || '').trim();
  const providers = normalizeProviders(input?.online_providers);
  const autoStrategy = normalizeAutoStrategy(input?.auto_transfer_subtitle_strategy);
  const embyLibraries = input?.emby_query_libraries == null
    ? null
    : (Array.isArray(input.emby_query_libraries) ? input.emby_query_libraries : []);
  const embyServers = Array.isArray(input?.emby_library_servers)
    ? input.emby_library_servers
    : (embyLibraries || []).map(item => item?.server);
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
    auto_skip_chinese_media_on_transfer: input?.auto_skip_chinese_media_on_transfer !== false,
    auto_transfer_subtitle_strategy: autoStrategy,
    trust_transfer_history_paths: Boolean(input?.trust_transfer_history_paths),
    manual_strm_enabled: Boolean(input?.manual_strm_enabled),
    manual_strm_paths: normalizeMultilinePaths(input?.manual_strm_paths),
    auto_search_on_manual_strm: Boolean(input?.auto_search_on_manual_strm),
    auto_multi_subtitle_mode: autoMultiSubtitleModes.some(item => item.value === input?.auto_multi_subtitle_mode)
      ? input.auto_multi_subtitle_mode
      : 'best',
    auto_subtitle_language_priority: normalizeLanguages(input?.auto_subtitle_language_priority),
    auto_subtitle_language_strict: Boolean(input?.auto_subtitle_language_strict),
    auto_subtitle_format_priority: normalizeList(
      input?.auto_subtitle_format_priority,
      autoFormatItems.map(item => item.value),
      ['.ass', '.srt', '.ssa', '.vtt'],
    ),
    auto_ass_to_srt_for_ai: input?.auto_ass_to_srt_for_ai !== false,
    timeline_max_offset_seconds: Math.min(300, Math.max(1, Math.round(normalizePositiveNumber(input?.timeline_max_offset_seconds, 120)))),
    timeline_min_offset_seconds: normalizePositiveNumber(input?.timeline_min_offset_seconds, 0.2),
    timeline_vad_mode: timelineVadItems.some(item => item.value === input?.timeline_vad_mode) ? input.timeline_vad_mode : 'webrtc',
    timeline_allow_risky_offset: Boolean(input?.timeline_allow_risky_offset),
    subhd_url: normalizeRootUrl(input?.subhd_url, 'https://subhd.tv'),
    subdl_api_key: String(input?.subdl_api_key || '').trim(),
    subhd_bypass_url: String(input?.subhd_bypass_url || '').trim().replace(/\/+$/, ''),
    subtitlecat_include_translations: Boolean(input?.subtitlecat_include_translations),
    assrt_url: normalizeRootUrl(input?.assrt_url, 'https://2.assrt.net'),
    assrt_api_key: assrtApiKey,
    assrt_api_url: normalizeRootUrl(input?.assrt_api_url, 'https://api.assrt.net'),
    opensubtitles_url: normalizeRootUrl(input?.opensubtitles_url, 'https://www.opensubtitles.com'),
    opensubtitles_api_key: opensubtitlesApiKey,
    opensubtitles_api_url: normalizeRootUrl(input?.opensubtitles_api_url, 'https://api.opensubtitles.com/api/v1'),
    opensubtitles_username: opensubtitlesUsername,
    opensubtitles_password: opensubtitlesPassword,
    ai_link_enabled: input?.ai_link_enabled !== false,
    rar_dependency_mode: ['none', 'container_install', 'mapped_binary'].includes(input?.rar_dependency_mode)
      ? input.rar_dependency_mode
      : 'none',
    rar_tool_path: String(input?.rar_tool_path || '/usr/bin/unar').trim() || '/usr/bin/unar',
  }
}

async function scanLocalDirectories() {
  directoryScanLoading.value = true;
  directoryScanNotice.value = '';
  directoryScanError.value = '';
  try {
    const pluginApi = createNeoSubtitleMasterApi(props.api, () => `plugin/${props.pluginId}`);
    const response = await pluginApi.scanLocalDirectories();
    if (response?.success === false) throw new Error(response.message || '检查已有视频失败')
    const result = unwrapResponse(response);
    directoryScanNotice.value = result.started
      ? '已开始检查。符合条件的目标会分批加入自动入库队列。'
      : '上一次检查仍在进行。';
  } catch (error) {
    directoryScanError.value = error?.response?.data?.detail || error?.message || '检查已有视频失败';
  } finally {
    directoryScanLoading.value = false;
  }
}

function saveConfig() {
  const normalized = normalizeConfig(localConfig.value);
  if (normalized.auto_subtitle_language_strict && !normalized.auto_subtitle_language_priority.length) {
    configError.value = '仅接受所选语言时，请至少选择一种语言。';
    return
  }
  if (normalized.opensubtitles_username.includes('@')) {
    configError.value = '请输入用户名而非邮箱！';
    return
  }
  configError.value = '';
  emit('save', normalized);
}

onMounted(() => {
  localConfig.value = normalizeConfig(props.initialConfig);
});

return (_ctx, _cache) => {
  const _component_VSpacer = _resolveComponent("VSpacer");
  const _component_VBtn = _resolveComponent("VBtn");
  const _component_VToolbar = _resolveComponent("VToolbar");
  const _component_VDivider = _resolveComponent("VDivider");
  const _component_VAlert = _resolveComponent("VAlert");
  const _component_VSwitch = _resolveComponent("VSwitch");
  const _component_VTextarea = _resolveComponent("VTextarea");
  const _component_VSelect = _resolveComponent("VSelect");
  const _component_v_icon = _resolveComponent("v-icon");
  const _component_VTextField = _resolveComponent("VTextField");
  const _component_VCardText = _resolveComponent("VCardText");
  const _component_VCard = _resolveComponent("VCard");

  return (_openBlock(), _createElementBlock("div", _hoisted_1, [
    _createElementVNode("div", _hoisted_2, [
      _createVNode(_component_VToolbar, {
        density: "comfortable",
        color: "transparent"
      }, {
        default: _withCtx(() => [
          _cache[45] || (_cache[45] = _createElementVNode("div", {
            class: "text-subtitle-1 ms-3 config-toolbar-title",
            title: "NEO字幕大师配置"
          }, "NEO字幕大师配置", -1)),
          _createVNode(_component_VSpacer),
          _createVNode(_component_VBtn, {
            href: "https://github.com/felix-klk/MoviePilot-Plugins/tree/main/plugins.v3/neosubtitlemaster#使用",
            target: "_blank",
            rel: "noopener noreferrer",
            "prepend-icon": "mdi-book-open-page-variant",
            variant: "text",
            color: "primary",
            class: "me-2"
          }, {
            default: _withCtx(() => [...(_cache[44] || (_cache[44] = [
              _createTextVNode(" 使用教程 ", -1)
            ]))]),
            _: 1
          }),
          _createVNode(_component_VBtn, {
            icon: "mdi-content-save",
            variant: "text",
            color: "primary",
            loading: props.saving,
            "aria-label": "保存配置",
            onClick: saveConfig
          }, null, 8, ["loading"]),
          _createVNode(_component_VBtn, {
            icon: "mdi-close",
            variant: "text",
            disabled: props.saving,
            "aria-label": "关闭配置",
            onClick: _cache[0] || (_cache[0] = $event => (emit('close')))
          }, null, 8, ["disabled"])
        ]),
        _: 1
      }),
      _createVNode(_component_VDivider)
    ]),
    _createElementVNode("div", _hoisted_3, [
      _createVNode(_component_VCard, {
        rounded: "xl",
        elevation: "0",
        class: "config-card"
      }, {
        default: _withCtx(() => [
          _createVNode(_component_VCardText, null, {
            default: _withCtx(() => [
              (configError.value)
                ? (_openBlock(), _createBlock(_component_VAlert, {
                    key: 0,
                    class: "mb-4",
                    type: "error",
                    variant: "tonal",
                    density: "compact",
                    text: configError.value
                  }, null, 8, ["text"]))
                : _createCommentVNode("", true),
              _cache[59] || (_cache[59] = _createElementVNode("div", { class: "config-section" }, [
                _createElementVNode("div", { class: "config-section-title" }, "基础设置")
              ], -1)),
              _createElementVNode("div", _hoisted_4, [
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.enabled,
                  "onUpdate:modelValue": _cache[1] || (_cache[1] = $event => ((localConfig.value.enabled) = $event)),
                  label: "启用插件",
                  color: "primary",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.show_sidebar_nav,
                  "onUpdate:modelValue": _cache[2] || (_cache[2] = $event => ((localConfig.value.show_sidebar_nav) = $event)),
                  label: "显示侧边栏入口",
                  color: "primary",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.ai_link_enabled,
                  "onUpdate:modelValue": _cache[3] || (_cache[3] = $event => ((localConfig.value.ai_link_enabled) = $event)),
                  label: "启用 AI 字幕联动",
                  color: "warning",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.traditional_to_simplified,
                  "onUpdate:modelValue": _cache[4] || (_cache[4] = $event => ((localConfig.value.traditional_to_simplified) = $event)),
                  label: "写入前繁体转简体",
                  color: "success",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.emby_refresh_after_subtitle_write,
                  "onUpdate:modelValue": _cache[5] || (_cache[5] = $event => ((localConfig.value.emby_refresh_after_subtitle_write) = $event)),
                  label: "写入后扫描 Emby 外挂字幕",
                  color: "info",
                  hint: "优先启动神医助手外挂字幕扫描任务；未找到任务时触发 Emby 全库刷新。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.auto_search_on_transfer,
                  "onUpdate:modelValue": _cache[6] || (_cache[6] = $event => ((localConfig.value.auto_search_on_transfer) = $event)),
                  label: "入库后自动搜索匹配字幕",
                  color: "info",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.auto_check_emby_subtitles,
                  "onUpdate:modelValue": _cache[7] || (_cache[7] = $event => ((localConfig.value.auto_check_emby_subtitles) = $event)),
                  label: "自动下载前检查 Emby 字幕",
                  color: "info",
                  hint: "本地无法读取内嵌字幕时，查询 MoviePilot 已配置的 Emby。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                (embyScopeEnabled.value)
                  ? (_openBlock(), _createBlock(_component_VTextarea, {
                      key: 0,
                      modelValue: localConfig.value.emby_subtitle_path_mappings,
                      "onUpdate:modelValue": _cache[8] || (_cache[8] = $event => ((localConfig.value.emby_subtitle_path_mappings) = $event)),
                      label: "Emby 路径映射",
                      placeholder: "/media => /mnt/media",
                      hint: "路径相同可留空。每行一组，左边填 MoviePilot 路径，右边填 Emby 路径。",
                      "persistent-hint": "",
                      rows: "2",
                      "auto-grow": "",
                      variant: "outlined",
                      density: "comfortable",
                      class: "config-grid-wide"
                    }, null, 8, ["modelValue"]))
                  : _createCommentVNode("", true),
                (embyScopeEnabled.value)
                  ? (_openBlock(), _createBlock(_component_VSelect, {
                      key: 1,
                      "model-value": localConfig.value.emby_query_libraries === null ? 'inherit' : 'selected',
                      items: embyScopeItems,
                      label: "Emby 查询范围",
                      hint: "范围用于检查字幕及写入后通知。实际扫描范围由 Emby 决定。",
                      "persistent-hint": "",
                      variant: "outlined",
                      density: "comfortable",
                      class: "config-grid-wide",
                      "onUpdate:modelValue": setEmbyScope
                    }, null, 8, ["model-value"]))
                  : _createCommentVNode("", true),
                (embyCustomScope.value)
                  ? (_openBlock(), _createElementBlock(_Fragment, { key: 2 }, [
                      _createVNode(_component_VSelect, {
                        modelValue: localConfig.value.emby_library_servers,
                        "onUpdate:modelValue": _cache[9] || (_cache[9] = $event => ((localConfig.value.emby_library_servers) = $event)),
                        items: embyServers.value,
                        loading: embyLibrariesLoading.value && !embyServersLoaded.value,
                        label: "Emby 服务器",
                        variant: "outlined",
                        density: "comfortable",
                        multiple: "",
                        chips: "",
                        "closable-chips": "",
                        "hide-details": ""
                      }, null, 8, ["modelValue", "items", "loading"]),
                      (!embyServersLoaded.value)
                        ? (_openBlock(), _createBlock(_component_VBtn, {
                            key: 0,
                            loading: embyLibrariesLoading.value,
                            variant: "tonal",
                            onClick: _cache[10] || (_cache[10] = $event => (readEmbyLibraries()))
                          }, {
                            default: _withCtx(() => [...(_cache[46] || (_cache[46] = [
                              _createTextVNode("读取服务器列表", -1)
                            ]))]),
                            _: 1
                          }, 8, ["loading"]))
                        : (_openBlock(), _createBlock(_component_VBtn, {
                            key: 1,
                            disabled: !localConfig.value.emby_library_servers.length,
                            loading: embyLibrariesLoading.value,
                            variant: "tonal",
                            onClick: _cache[11] || (_cache[11] = $event => (readEmbyLibraries(localConfig.value.emby_library_servers)))
                          }, {
                            default: _withCtx(() => [...(_cache[47] || (_cache[47] = [
                              _createTextVNode("读取所选服务器的媒体库", -1)
                            ]))]),
                            _: 1
                          }, 8, ["disabled", "loading"])),
                      (embyLibrariesError.value)
                        ? (_openBlock(), _createBlock(_component_VAlert, {
                            key: 2,
                            type: "error",
                            variant: "tonal",
                            class: "config-grid-wide",
                            text: embyLibrariesError.value
                          }, null, 8, ["text"]))
                        : _createCommentVNode("", true),
                      _createVNode(_component_VSelect, {
                        modelValue: embySelectedKeys.value,
                        "onUpdate:modelValue": _cache[12] || (_cache[12] = $event => ((embySelectedKeys).value = $event)),
                        items: embyLibraryItems.value,
                        label: "参与查询的媒体库",
                        hint: embySelectedKeys.value.length ? '未选媒体库的服务器会被跳过。' : '未选择媒体库，字幕检查仅使用本地信息。',
                        "no-data-text": "请先读取需要使用的服务器的媒体库",
                        "persistent-hint": "",
                        multiple: "",
                        chips: "",
                        "closable-chips": "",
                        clearable: "",
                        variant: "outlined",
                        density: "comfortable",
                        class: "config-grid-wide"
                      }, null, 8, ["modelValue", "items", "hint"])
                    ], 64))
                  : _createCommentVNode("", true),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.auto_skip_chinese_media_on_transfer,
                  "onUpdate:modelValue": _cache[13] || (_cache[13] = $event => ((localConfig.value.auto_skip_chinese_media_on_transfer) = $event)),
                  label: "入库自动处理跳过中文资源",
                  color: "success",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.trust_transfer_history_paths,
                  "onUpdate:modelValue": _cache[14] || (_cache[14] = $event => ((localConfig.value.trust_transfer_history_paths) = $event)),
                  label: "信任整理历史路径",
                  color: "warning",
                  hint: "CD2、网盘挂载、SMB 等慢路径可开启，刷新资源清单时不逐条访问文件。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.manual_strm_enabled,
                  "onUpdate:modelValue": _cache[15] || (_cache[15] = $event => ((localConfig.value.manual_strm_enabled) = $event)),
                  label: "监控本地整理目录",
                  color: "info",
                  hint: "递归监控视频、STRM、NFO 和同目录字幕。开启时建立基线。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                (localConfig.value.manual_strm_enabled)
                  ? (_openBlock(), _createBlock(_component_VTextarea, {
                      key: 3,
                      modelValue: localConfig.value.manual_strm_paths,
                      "onUpdate:modelValue": _cache[16] || (_cache[16] = $event => ((localConfig.value.manual_strm_paths) = $event)),
                      label: "本地整理目录",
                      placeholder: manualStrmPathPlaceholder,
                      rows: "3",
                      "auto-grow": "",
                      hint: "每行一个 MoviePilot 可见的绝对路径，例如 Windows 的 E:\\test 或容器内的 /media/test。",
                      "persistent-hint": "",
                      variant: "outlined",
                      density: "comfortable",
                      class: "config-grid-wide"
                    }, null, 8, ["modelValue"]))
                  : _createCommentVNode("", true),
                (localConfig.value.manual_strm_enabled)
                  ? (_openBlock(), _createBlock(_component_VSwitch, {
                      key: 4,
                      modelValue: localConfig.value.auto_search_on_manual_strm,
                      "onUpdate:modelValue": _cache[17] || (_cache[17] = $event => ((localConfig.value.auto_search_on_manual_strm) = $event)),
                      label: "缺字幕时自动补搜",
                      color: "success",
                      hint: "视频稳定后或对应外挂移除时，按当前语言设置检查并补搜。",
                      "persistent-hint": ""
                    }, null, 8, ["modelValue"]))
                  : _createCommentVNode("", true),
                (localConfig.value.manual_strm_enabled && props.api?.post)
                  ? (_openBlock(), _createElementBlock("div", _hoisted_5, [
                      _createVNode(_component_VBtn, {
                        variant: "tonal",
                        loading: directoryScanLoading.value,
                        onClick: scanLocalDirectories
                      }, {
                        default: _withCtx(() => [...(_cache[48] || (_cache[48] = [
                          _createTextVNode(" 检查已有视频 ", -1)
                        ]))]),
                        _: 1
                      }, 8, ["loading"]),
                      _cache[49] || (_cache[49] = _createElementVNode("p", { class: "text-caption mt-2" }, "使用已保存的目录和语言设置。目录离线时，恢复后继续。", -1)),
                      (directoryScanNotice.value)
                        ? (_openBlock(), _createBlock(_component_VAlert, {
                            key: 0,
                            type: "info",
                            variant: "tonal",
                            class: "mt-2",
                            role: "status"
                          }, {
                            default: _withCtx(() => [
                              _createTextVNode(_toDisplayString(directoryScanNotice.value), 1)
                            ]),
                            _: 1
                          }))
                        : _createCommentVNode("", true),
                      (directoryScanError.value)
                        ? (_openBlock(), _createBlock(_component_VAlert, {
                            key: 1,
                            type: "error",
                            variant: "tonal",
                            class: "mt-2"
                          }, {
                            default: _withCtx(() => [
                              _createTextVNode(_toDisplayString(directoryScanError.value), 1)
                            ]),
                            _: 1
                          }))
                        : _createCommentVNode("", true)
                    ]))
                  : _createCommentVNode("", true),
                _createVNode(_component_VSelect, {
                  modelValue: localConfig.value.auto_transfer_subtitle_strategy,
                  "onUpdate:modelValue": _cache[18] || (_cache[18] = $event => ((localConfig.value.auto_transfer_subtitle_strategy) = $event)),
                  items: autoStrategyItems,
                  label: "入库后字幕处理策略",
                  variant: "outlined",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSelect, {
                  modelValue: localConfig.value.auto_multi_subtitle_mode,
                  "onUpdate:modelValue": _cache[19] || (_cache[19] = $event => ((localConfig.value.auto_multi_subtitle_mode) = $event)),
                  items: autoMultiSubtitleModes,
                  label: "自动多字幕处理",
                  variant: "outlined",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSelect, {
                  modelValue: localConfig.value.auto_subtitle_language_priority,
                  "onUpdate:modelValue": _cache[20] || (_cache[20] = $event => ((localConfig.value.auto_subtitle_language_priority) = $event)),
                  items: autoLanguageItems,
                  label: "语言优先级",
                  variant: "outlined",
                  density: "comfortable",
                  multiple: "",
                  chips: "",
                  hint: "选了双语，已有字幕须满足所选双语之一，否则补充搜索；多选按顺序优先，找不到时不回退单语或 AI。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                (localConfig.value.auto_subtitle_language_priority.length)
                  ? (_openBlock(), _createElementBlock("details", _hoisted_6, [
                      _createElementVNode("summary", null, [
                        _createElementVNode("span", _hoisted_7, [
                          _cache[50] || (_cache[50] = _createElementVNode("span", null, "调整语言顺序", -1)),
                          _createElementVNode("span", _hoisted_8, _toDisplayString(localConfig.value.auto_subtitle_language_priority.length) + " 项已选，序号 1 优先 ", 1)
                        ]),
                        _createVNode(_component_v_icon, {
                          icon: "mdi-chevron-down",
                          size: "18",
                          class: "language-priority-chevron",
                          "aria-hidden": "true"
                        })
                      ]),
                      _createElementVNode("div", _hoisted_9, [
                        (_openBlock(true), _createElementBlock(_Fragment, null, _renderList(localConfig.value.auto_subtitle_language_priority, (language, index) => {
                          return (_openBlock(), _createElementBlock("div", {
                            key: language,
                            class: _normalizeClass(["language-priority-row", { 'is-drop-target': dropTargetAutoLanguage.value === language, 'is-dragging': draggingAutoLanguage.value === language }]),
                            onDragover: _withModifiers($event => (dropTargetAutoLanguage.value = language), ["prevent"]),
                            onDrop: $event => (dropAutoLanguage(language, $event))
                          }, [
                            _createElementVNode("button", {
                              type: "button",
                              class: "language-priority-drag-handle",
                              "aria-label": '拖动' + autoLanguageTitle(language) + '调整顺序',
                              title: "拖动调整顺序",
                              draggable: "true",
                              onDragstart: $event => (startDraggingAutoLanguage(language, $event)),
                              onDragend: finishDraggingAutoLanguage
                            }, [
                              _createVNode(_component_v_icon, {
                                icon: "mdi-drag",
                                size: "18",
                                "aria-hidden": "true"
                              })
                            ], 40, _hoisted_11),
                            _createElementVNode("span", _hoisted_12, _toDisplayString(autoLanguageTitle(language)), 1),
                            _createElementVNode("input", {
                              value: index + 1,
                              "aria-label": `${autoLanguageTitle(language)}序号`,
                              type: "number",
                              min: "1",
                              max: localConfig.value.auto_subtitle_language_priority.length,
                              step: "1",
                              class: "language-priority-rank",
                              onChange: $event => (setAutoLanguageRank(language, $event.target.value))
                            }, null, 40, _hoisted_13)
                          ], 42, _hoisted_10))
                        }), 128))
                      ])
                    ]))
                  : _createCommentVNode("", true),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.auto_subtitle_language_strict,
                  "onUpdate:modelValue": _cache[21] || (_cache[21] = $event => ((localConfig.value.auto_subtitle_language_strict) = $event)),
                  label: "仅接受所选语言",
                  color: "info",
                  hint: "未选择双语时，开启此项才限制为所选语言；无匹配不回退或提交 AI 翻译。双语要求始终生效。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSelect, {
                  modelValue: localConfig.value.auto_subtitle_format_priority,
                  "onUpdate:modelValue": _cache[22] || (_cache[22] = $event => ((localConfig.value.auto_subtitle_format_priority) = $event)),
                  items: autoFormatItems,
                  label: "格式优先级",
                  variant: "outlined",
                  density: "comfortable",
                  multiple: "",
                  chips: "",
                  hint: "默认：ASS、SRT、SSA、VTT",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.auto_ass_to_srt_for_ai,
                  "onUpdate:modelValue": _cache[23] || (_cache[23] = $event => ((localConfig.value.auto_ass_to_srt_for_ai) = $event)),
                  label: "英文 ASS 转临时 SRT 后提交 AI",
                  color: "info",
                  "hide-details": ""
                }, null, 8, ["modelValue"])
              ]),
              _createVNode(_component_VDivider, { class: "my-5" }),
              _cache[60] || (_cache[60] = _createElementVNode("div", { class: "config-section" }, [
                _createElementVNode("div", null, [
                  _createElementVNode("div", { class: "config-section-title" }, "在线字幕搜索"),
                  _createElementVNode("p", null, "先选择启用的字幕源，再填写对应平台的设置。站点暂时不可用时，可在搜索页手动跳转。")
                ])
              ], -1)),
              _createElementVNode("div", _hoisted_14, [
                _createElementVNode("div", _hoisted_15, [
                  _createVNode(_component_VSelect, {
                    modelValue: localConfig.value.online_providers,
                    "onUpdate:modelValue": _cache[24] || (_cache[24] = $event => ((localConfig.value.online_providers) = $event)),
                    items: _unref(onlineProviderItems),
                    label: "启用字幕源",
                    variant: "outlined",
                    density: "comfortable",
                    multiple: "",
                    chips: "",
                    "hide-details": ""
                  }, null, 8, ["modelValue", "items"]),
                  _createVNode(_component_VSelect, {
                    modelValue: localConfig.value.online_use_proxy,
                    "onUpdate:modelValue": _cache[25] || (_cache[25] = $event => ((localConfig.value.online_use_proxy) = $event)),
                    items: [
                  { title: '不使用系统代理', value: false },
                  { title: '使用 MoviePilot 系统代理', value: true },
                ],
                    label: "API 请求代理",
                    variant: "outlined",
                    density: "comfortable",
                    "hide-details": ""
                  }, null, 8, ["modelValue"])
                ]),
                _cache[58] || (_cache[58] = _createElementVNode("p", { class: "provider-note online-source-help" }, "填写密钥后，仍需在上方“启用字幕源”中勾选对应平台，并保存设置。", -1)),
                _createElementVNode("section", _hoisted_16, [
                  _cache[51] || (_cache[51] = _createElementVNode("div", { class: "provider-heading" }, [
                    _createElementVNode("h3", { id: "provider-subhd-title" }, "SubHD"),
                    _createElementVNode("p", null, "无需 API Key")
                  ], -1)),
                  _createElementVNode("div", _hoisted_17, [
                    _createElementVNode("div", _hoisted_18, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.subhd_url,
                        "onUpdate:modelValue": _cache[26] || (_cache[26] = $event => ((localConfig.value.subhd_url) = $event)),
                        label: "SubHD 站点地址",
                        placeholder: "https://subhd.tv",
                        variant: "outlined",
                        density: "comfortable",
                        "hide-details": ""
                      }, null, 8, ["modelValue"]),
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.subhd_bypass_url,
                        "onUpdate:modelValue": _cache[27] || (_cache[27] = $event => ((localConfig.value.subhd_bypass_url) = $event)),
                        label: "SubHD CF 服务地址",
                        placeholder: "http://NAS地址:18089",
                        hint: "填写后 SubHD 通过此服务自身网络访问，不继承 MP 系统代理。Turnstile 下载验证需要服务支持 /turnstile；留空直接访问。",
                        "persistent-hint": "",
                        variant: "outlined",
                        density: "comfortable"
                      }, null, 8, ["modelValue"])
                    ])
                  ])
                ]),
                _createElementVNode("section", _hoisted_19, [
                  _cache[52] || (_cache[52] = _createElementVNode("div", { class: "provider-heading" }, [
                    _createElementVNode("h3", { id: "provider-subtitlecat-title" }, "SubtitleCat"),
                    _createElementVNode("p", null, "无需 API Key")
                  ], -1)),
                  _createElementVNode("div", _hoisted_20, [
                    _createElementVNode("div", _hoisted_21, [
                      _createVNode(_component_VSwitch, {
                        modelValue: localConfig.value.subtitlecat_include_translations,
                        "onUpdate:modelValue": _cache[28] || (_cache[28] = $event => ((localConfig.value.subtitlecat_include_translations) = $event)),
                        label: "接受已有中文站点翻译",
                        hint: "默认只搜索原字幕；开启后另列站点翻译，不会发起在线翻译任务。",
                        "persistent-hint": "",
                        color: "primary"
                      }, null, 8, ["modelValue"])
                    ])
                  ])
                ]),
                _createElementVNode("section", _hoisted_22, [
                  _cache[54] || (_cache[54] = _createElementVNode("div", { class: "provider-heading" }, [
                    _createElementVNode("h3", { id: "provider-subdl-title" }, "SubDL"),
                    _createElementVNode("p", null, "需要 API Key")
                  ], -1)),
                  _createElementVNode("div", _hoisted_23, [
                    _createElementVNode("div", _hoisted_24, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.subdl_api_key,
                        "onUpdate:modelValue": _cache[29] || (_cache[29] = $event => ((localConfig.value.subdl_api_key) = $event)),
                        label: "SubDL API Key",
                        placeholder: "粘贴从 SubDL 账户获取的密钥",
                        type: "password",
                        autocomplete: "new-password",
                        variant: "outlined",
                        density: "comfortable",
                        "hide-details": ""
                      }, null, 8, ["modelValue"])
                    ]),
                    _cache[53] || (_cache[53] = _createElementVNode("p", { class: "provider-note" }, [
                      _createElementVNode("a", {
                        href: "https://subdl.com/panel/api",
                        target: "_blank",
                        rel: "noopener noreferrer",
                        class: "provider-help-link"
                      }, "获取 API Key ↗"),
                      _createElementVNode("span", null, "注册或登录 SubDL 后，在 API 管理页获取密钥。")
                    ], -1))
                  ])
                ]),
                _createElementVNode("section", _hoisted_25, [
                  _cache[55] || (_cache[55] = _createElementVNode("div", { class: "provider-heading" }, [
                    _createElementVNode("h3", { id: "provider-assrt-title" }, "射手网（伪） / ASSRT"),
                    _createElementVNode("p", null, "需要 API Key")
                  ], -1)),
                  _createElementVNode("div", _hoisted_26, [
                    _createElementVNode("div", _hoisted_27, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.assrt_api_key,
                        "onUpdate:modelValue": _cache[30] || (_cache[30] = $event => ((localConfig.value.assrt_api_key) = $event)),
                        label: "射手网(伪) API Key",
                        placeholder: "未填写时默认不启用伪射手自动搜索",
                        variant: "outlined",
                        density: "comfortable",
                        type: "password",
                        autocomplete: "new-password",
                        "hide-details": ""
                      }, null, 8, ["modelValue"])
                    ]),
                    _createElementVNode("div", _hoisted_28, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.assrt_api_url,
                        "onUpdate:modelValue": _cache[31] || (_cache[31] = $event => ((localConfig.value.assrt_api_url) = $event)),
                        label: "射手网(伪) API 地址",
                        placeholder: "https://api.assrt.net",
                        variant: "outlined",
                        density: "comfortable",
                        "hide-details": ""
                      }, null, 8, ["modelValue"]),
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.assrt_url,
                        "onUpdate:modelValue": _cache[32] || (_cache[32] = $event => ((localConfig.value.assrt_url) = $event)),
                        label: "射手网(伪) 手动搜索地址",
                        placeholder: "https://2.assrt.net",
                        variant: "outlined",
                        density: "comfortable",
                        "hide-details": ""
                      }, null, 8, ["modelValue"])
                    ])
                  ])
                ]),
                _createElementVNode("section", _hoisted_29, [
                  _cache[57] || (_cache[57] = _createElementVNode("div", { class: "provider-heading" }, [
                    _createElementVNode("h3", { id: "provider-opensubtitles-title" }, "OpenSubtitles"),
                    _createElementVNode("p", null, "搜索需要 API Key；下载需账号")
                  ], -1)),
                  _createElementVNode("div", _hoisted_30, [
                    _createElementVNode("div", _hoisted_31, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.opensubtitles_api_key,
                        "onUpdate:modelValue": _cache[33] || (_cache[33] = $event => ((localConfig.value.opensubtitles_api_key) = $event)),
                        label: "OpenSubtitles API Key",
                        placeholder: "用于搜索多语言字幕",
                        variant: "outlined",
                        density: "comfortable",
                        type: "password",
                        autocomplete: "new-password",
                        "hide-details": ""
                      }, null, 8, ["modelValue"])
                    ]),
                    _createElementVNode("div", _hoisted_32, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.opensubtitles_username,
                        "onUpdate:modelValue": _cache[34] || (_cache[34] = $event => ((localConfig.value.opensubtitles_username) = $event)),
                        label: "OpenSubtitles 用户名（可选）",
                        placeholder: "下载时用于后台登录换取 token",
                        variant: "outlined",
                        density: "comfortable",
                        autocomplete: "username",
                        "hide-details": "auto",
                        error: localConfig.value.opensubtitles_username.includes('@'),
                        "error-messages": localConfig.value.opensubtitles_username.includes('@') ? '请输入用户名而非邮箱！' : ''
                      }, null, 8, ["modelValue", "error", "error-messages"]),
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.opensubtitles_password,
                        "onUpdate:modelValue": _cache[35] || (_cache[35] = $event => ((localConfig.value.opensubtitles_password) = $event)),
                        label: "OpenSubtitles 密码（可选）",
                        placeholder: "下载时用于后台登录换取 token",
                        variant: "outlined",
                        density: "comfortable",
                        type: "password",
                        autocomplete: "new-password",
                        "hide-details": ""
                      }, null, 8, ["modelValue"])
                    ]),
                    _createElementVNode("div", _hoisted_33, [
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.opensubtitles_api_url,
                        "onUpdate:modelValue": _cache[36] || (_cache[36] = $event => ((localConfig.value.opensubtitles_api_url) = $event)),
                        label: "OpenSubtitles API 地址",
                        placeholder: "https://api.opensubtitles.com/api/v1",
                        variant: "outlined",
                        density: "comfortable",
                        "hide-details": ""
                      }, null, 8, ["modelValue"]),
                      _createVNode(_component_VTextField, {
                        modelValue: localConfig.value.opensubtitles_url,
                        "onUpdate:modelValue": _cache[37] || (_cache[37] = $event => ((localConfig.value.opensubtitles_url) = $event)),
                        label: "OpenSubtitles 手动搜索地址",
                        placeholder: "https://www.opensubtitles.com",
                        variant: "outlined",
                        density: "comfortable",
                        "hide-details": ""
                      }, null, 8, ["modelValue"])
                    ]),
                    _cache[56] || (_cache[56] = _createElementVNode("p", { class: "provider-note" }, "下载时使用用户名和密码登录。下载后的英文字幕，可提交给 AI 字幕生成翻译。", -1))
                  ])
                ])
              ]),
              _createVNode(_component_VDivider, { class: "my-5" }),
              _cache[61] || (_cache[61] = _createElementVNode("div", { class: "config-section" }, [
                _createElementVNode("div", null, [
                  _createElementVNode("div", { class: "config-section-title" }, "智能调轴"),
                  _createElementVNode("p", null, "控制写入前可接受的全局偏移范围；超过 120 秒通常意味着错集、错版本或整季包映射错误。")
                ])
              ], -1)),
              _createElementVNode("div", _hoisted_34, [
                _createVNode(_component_VTextField, {
                  modelValue: localConfig.value.timeline_max_offset_seconds,
                  "onUpdate:modelValue": _cache[38] || (_cache[38] = $event => ((localConfig.value.timeline_max_offset_seconds) = $event)),
                  modelModifiers: { number: true },
                  label: "智能调轴最大偏移秒数",
                  type: "number",
                  min: "1",
                  max: "300",
                  suffix: "秒",
                  hint: "默认 120；不建议超过 120 秒。超过 120 秒的手动操作会再次确认，自动入库不放行高风险结果。",
                  "persistent-hint": "",
                  variant: "outlined",
                  density: "comfortable"
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VTextField, {
                  modelValue: localConfig.value.timeline_min_offset_seconds,
                  "onUpdate:modelValue": _cache[39] || (_cache[39] = $event => ((localConfig.value.timeline_min_offset_seconds) = $event)),
                  modelModifiers: { number: true },
                  label: "最小应用阈值",
                  type: "number",
                  min: "0.1",
                  step: "0.1",
                  suffix: "秒",
                  hint: "低于该阈值时仅复制字幕，不做时间轴改写。",
                  "persistent-hint": "",
                  variant: "outlined",
                  density: "comfortable"
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSelect, {
                  modelValue: localConfig.value.timeline_vad_mode,
                  "onUpdate:modelValue": _cache[40] || (_cache[40] = $event => ((localConfig.value.timeline_vad_mode) = $event)),
                  items: timelineVadItems,
                  label: "音频 VAD 模式",
                  variant: "outlined",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VSwitch, {
                  modelValue: localConfig.value.timeline_allow_risky_offset,
                  "onUpdate:modelValue": _cache[41] || (_cache[41] = $event => ((localConfig.value.timeline_allow_risky_offset) = $event)),
                  label: "全局允许高风险偏移",
                  color: "warning",
                  hint: "不建议开启。开启后后端会允许 120 秒以上结果；手动操作仍会显示风险确认。",
                  "persistent-hint": ""
                }, null, 8, ["modelValue"])
              ]),
              _createVNode(_component_VDivider, { class: "my-5" }),
              _cache[62] || (_cache[62] = _createElementVNode("div", { class: "config-section" }, [
                _createElementVNode("div", null, [
                  _createElementVNode("div", { class: "config-section-title" }, "RAR / 7Z 解压器"),
                  _createElementVNode("p", null, [
                    _createTextVNode("RAR / 7Z 默认使用 MoviePilot 容器内的 "),
                    _createElementVNode("code", null, "/usr/bin/unar"),
                    _createTextVNode("。")
                  ])
                ])
              ], -1)),
              _createElementVNode("div", _hoisted_35, [
                _createVNode(_component_VSelect, {
                  modelValue: localConfig.value.rar_dependency_mode,
                  "onUpdate:modelValue": _cache[42] || (_cache[42] = $event => ((localConfig.value.rar_dependency_mode) = $event)),
                  items: rarDependencyModes,
                  label: "压缩包解压器处理方式",
                  variant: "outlined",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"]),
                _createVNode(_component_VTextField, {
                  modelValue: localConfig.value.rar_tool_path,
                  "onUpdate:modelValue": _cache[43] || (_cache[43] = $event => ((localConfig.value.rar_tool_path) = $event)),
                  label: "容器内映射路径",
                  placeholder: "/usr/bin/unar",
                  variant: "outlined",
                  density: "comfortable",
                  "hide-details": ""
                }, null, 8, ["modelValue"])
              ]),
              _createVNode(_component_VAlert, {
                class: "mt-4",
                type: "info",
                variant: "tonal",
                text: "当前 MoviePilot 容器内置 unar 时无需额外安装；如使用映射文件，请把 unar 映射到容器内 /usr/bin/unar。插件不会主动重启 Docker 容器。"
              })
            ]),
            _: 1
          })
        ]),
        _: 1
      })
    ])
  ]))
}
}

};
const Config = /*#__PURE__*/_export_sfc(_sfc_main, [['__scopeId',"data-v-f970fca6"]]);

export { buildOutputName as A, rarDependencyModeLabel as B, _export_sfc as _, onlineLanguageItems as a, onlineProviderItems as b, onlineResultKey as c, defaultOnlineProviderIds as d, Config as default, isForeignOnlineResult as e, providerPriority as f, onlineResultLanguagePriority as g, onlineResultIdentityPriority as h, isOnlineResultDownloadable as i, errorMessage as j, createNeoSubtitleMasterApi as k, formatBytes as l, compactTargetName as m, historyMediaStat as n, onlineResultLanguageFilterCategory as o, providerName as p, formatMediaType as q, mediaLabel as r, seasonLabel as s, timelineMetaItems as t, unwrapResponse as u, timelineResultText as v, mediaStat as w, onlineResultMeta as x, providerProgressText as y, providerProgressColor as z };
