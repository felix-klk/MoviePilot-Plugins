<script setup>
const downloadCountFormat = new Intl.NumberFormat('zh-CN')
const uploadTimeFormat = new Intl.DateTimeFormat('zh-CN', {
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  hourCycle: 'h23',
})

function formatUploadTime(value) {
  const timestamp = Date.parse(value)
  return Number.isNaN(timestamp) ? '' : uploadTimeFormat.format(timestamp).replaceAll('/', '-')
}

defineProps({
  modelValue: { type: Boolean, default: false },
  mobile: { type: Boolean, default: false },
  onlineTitle: { type: String, default: '' },
  onlineTargets: { type: Array, default: () => [] },
  selectedOnlineResults: { type: Array, default: () => [] },
  onlineAiDownloading: { type: Boolean, default: false },
  onlinePreviewDownloading: { type: Boolean, default: false },
  canSubmitOnlineAiTranslate: { type: Boolean, default: false },
  onlineDownloading: { type: Boolean, default: false },
  onlineKeyword: { type: String, default: '' },
  onlineSelectedProviders: { type: Array, default: () => [] },
  onlineProviderItems: { type: Array, default: () => [] },
  onlineSearching: { type: Boolean, default: false },
  onlineError: { type: String, default: '' },
  onlineMessages: { type: Array, default: () => [] },
  onlineMessagesCollapsed: { type: Boolean, default: false },
  onlineMessageType: { type: String, default: 'info' },
  onlineMessageSummary: { type: String, default: '' },
  hasOnlineResults: { type: Boolean, default: false },
  filteredOnlineResults: { type: Array, default: () => [] },
  onlineResults: { type: Array, default: () => [] },
  onlineLanguageFilter: { type: String, default: 'all' },
  onlineLanguageFilterItems: { type: Array, default: () => [] },
  onlineProviderFilter: { type: String, default: 'all' },
  onlineProviderFilterItems: { type: Array, default: () => [] },
  onlineProviderProgressItems: { type: Array, default: () => [] },
  selectedOnlineResultIds: { type: Array, default: () => [] },
  onlineManualLinks: { type: Array, default: () => [] },
  onlineAiConfirmDialog: { type: Boolean, default: false },
  onlineAiConfirmText: { type: String, default: '' },
  providerProgressColor: { type: Function, required: true },
  providerProgressText: { type: Function, required: true },
  providerName: { type: Function, required: true },
  onlineResultKey: { type: Function, required: true },
  onlineResultMeta: { type: Function, required: true },
  isOnlineResultDownloadable: { type: Function, required: true },
})

defineEmits([
  'update:modelValue',
  'update:onlineKeyword',
  'update:onlineSelectedProviders',
  'update:onlineMessagesCollapsed',
  'update:onlineLanguageFilter',
  'update:onlineProviderFilter',
  'update:onlineAiConfirmDialog',
  'download-online-preview',
  'request-online-ai-translate',
  'stop-online-download',
  'close-online-dialog',
  'run-online-search',
  'stop-online-search',
  'toggle-online-result',
  'confirm-online-ai-translate',
])
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="mobile"
    scrollable
    max-width="1080"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <VCard
      class="online-dialog"
      :class="{ 'smu-mobile-dialog-card': mobile }"
      :rounded="mobile ? 0 : 'xl'"
    >
      <VCardTitle class="dialog-title" :class="{ 'mobile-online-dialog-title': mobile }">
        <VBtn
          v-if="mobile"
          class="mobile-online-back-btn"
          icon="mdi-arrow-left"
          variant="text"
          title="退出搜索"
          @click="$emit('close-online-dialog')"
        />
        <div class="online-dialog-heading">
          <span>{{ onlineTitle || '在线字幕搜索' }}</span>
          <p>{{ onlineTargets.length }} 个目标 · 下载会进入匹配预览，提交 AI 翻译会直接进入 AI 状态</p>
        </div>
        <VBtn
          v-if="!mobile"
          icon="mdi-close"
          variant="text"
          aria-label="关闭在线字幕搜索"
          @click="$emit('close-online-dialog')"
        />
      </VCardTitle>
      <VDivider />
      <VCardActions class="online-search-actions">
        <VTextField
          :model-value="onlineKeyword"
          label="手动关键词（可选）"
          placeholder="留空按资源名、季集号自动生成"
          variant="outlined"
          density="comfortable"
          hide-details
          clearable
          @update:model-value="$emit('update:onlineKeyword', $event)"
          @keyup.enter="$emit('run-online-search')"
        />
        <VSelect
          :model-value="onlineSelectedProviders"
          :items="onlineProviderItems"
          label="字幕源"
          variant="outlined"
          density="comfortable"
          hide-details
          multiple
          chips
          @update:model-value="$emit('update:onlineSelectedProviders', $event)"
        />
        <VBtn
          color="primary"
          :disabled="!onlineSelectedProviders.length"
          :loading="onlineSearching"
          @click="$emit('run-online-search')"
        >
          搜索
        </VBtn>
        <VBtn
          v-if="onlineSearching"
          color="warning"
          variant="tonal"
          @click="$emit('stop-online-search')"
        >
          停止等待
        </VBtn>
      </VCardActions>
      <VDivider />
      <VCardText class="online-scroll-body">
        <VAlert
          v-if="onlineError"
          class="mb-4"
          type="error"
          variant="tonal"
          :text="onlineError"
        />
        <VAlert
          v-if="onlineMessages.length && !onlineMessagesCollapsed"
          class="online-message-summary"
          :type="onlineMessageType"
          variant="tonal"
          density="compact"
        >
          <div class="online-message-summary-content">
            <span>{{ onlineMessageSummary }}</span>
            <VBtn
              size="x-small"
              variant="text"
              @click="$emit('update:onlineMessagesCollapsed', true)"
            >
              收起
            </VBtn>
          </div>
        </VAlert>

        <div class="online-layout">
          <section class="online-results-panel">
            <div class="online-panel-head">
              <div>
                <div class="section-kicker">自动搜索</div>
                <h3>选择要下载的字幕</h3>
              </div>
              <span>{{ hasOnlineResults ? `${filteredOnlineResults.length}/${onlineResults.length} 条结果` : '暂无结果' }}</span>
            </div>
            <p v-if="hasOnlineResults" class="online-filter-hint">语言、来源各选一项，同时满足；中文仅含单语，双语单独分类。</p>
            <VChipGroup
              v-if="hasOnlineResults"
              :model-value="onlineLanguageFilter"
              class="online-provider-filter"
              column
              mandatory
              selected-class="online-provider-filter-active"
              @update:model-value="$emit('update:onlineLanguageFilter', $event)"
            >
              <VChip
                v-for="item in onlineLanguageFilterItems"
                :key="item.value"
                :value="item.value"
                size="small"
                variant="tonal"
              >
                {{ item.title }}
              </VChip>
            </VChipGroup>
            <VChipGroup
              v-if="hasOnlineResults"
              :model-value="onlineProviderFilter"
              class="online-provider-filter"
              column
              mandatory
              selected-class="online-provider-filter-active"
              @update:model-value="$emit('update:onlineProviderFilter', $event)"
            >
              <VChip
                v-for="item in onlineProviderFilterItems"
                :key="item.value"
                :value="item.value"
                size="small"
                variant="tonal"
              >
                {{ item.title }}
              </VChip>
            </VChipGroup>
            <div v-if="onlineProviderProgressItems.length" class="online-provider-progress">
              <VChip
                v-for="item in onlineProviderProgressItems"
                :key="item.provider"
                size="small"
                variant="tonal"
                :color="providerProgressColor(item.state)"
              >
                {{ providerName(item.provider) }} · {{ providerProgressText(item.state) }}
              </VChip>
            </div>

            <div v-if="onlineSearching && !filteredOnlineResults.length" class="online-loading">
              {{ mobile ? '正在搜索字幕' : '正在从 API 搜索字幕，先返回的结果会先显示...' }}
            </div>
            <div v-if="filteredOnlineResults.length" class="online-result-list">
              <div
                v-for="item in filteredOnlineResults"
                :key="onlineResultKey(item)"
                class="online-result-card"
                :class="{
                  active: selectedOnlineResultIds.includes(onlineResultKey(item)),
                  disabled: !isOnlineResultDownloadable(item),
                }"
              >
                <VCheckbox
                  :model-value="selectedOnlineResultIds.includes(onlineResultKey(item))"
                  density="compact"
                  hide-details
                  :disabled="!isOnlineResultDownloadable(item)"
                  @update:model-value="value => $emit('toggle-online-result', item, value)"
                />
                <div class="online-result-main">
                  <div class="online-result-title">{{ item.title }}</div>
                  <div class="online-result-meta">
                    <span>{{ providerName(item.provider) }}</span>
                    <span>{{ onlineResultMeta(item) }}</span>
                    <span v-if="item.subtitle_type" class="online-listing-metadata">
                      {{ item.subtitle_type }}
                    </span>
                    <span v-if="Number.isSafeInteger(item.download_count) && item.download_count >= 0" class="online-listing-metadata">
                      下载 {{ downloadCountFormat.format(item.download_count) }} 次
                    </span>
                    <time v-if="formatUploadTime(item.uploaded_at)" :datetime="item.uploaded_at" class="online-listing-metadata">
                      上传 {{ formatUploadTime(item.uploaded_at) }}
                    </time>
                    <span v-if="!isOnlineResultDownloadable(item)" class="online-manual-badge">
                      需手动下载
                    </span>
                  </div>
                  <p v-if="item.note">{{ item.note }}</p>
                  <p v-if="item.match_detail" class="online-match-detail">{{ item.match_detail }}</p>
                </div>
                <div class="online-result-actions">
                  <a
                    v-if="item.page_url"
                    class="online-open-link"
                    :href="item.page_url"
                    target="_blank"
                    rel="noopener noreferrer"
                    :aria-label="`查看 ${item.title}（新标签页）`"
                  >
                    查看
                  </a>
                </div>
              </div>
            </div>
            <div v-else-if="!onlineSearching" class="empty-state">
              {{ hasOnlineResults ? '当前平台筛选下没有结果。' : (mobile ? '搜索无结果，请手动搜索' : '没有可自动下载的字幕结果。可以换关键词重试，或使用右侧手动搜索。') }}
            </div>
          </section>

          <aside class="manual-links-panel">
            <div class="section-kicker">手动搜索</div>
            <h3>跳转字幕站</h3>
            <p>自动搜索失败或源站需要验证时，可打开链接下载字幕包后回到本页上传。</p>
            <div
              v-for="provider in onlineManualLinks"
              :key="provider.provider"
              class="manual-provider"
            >
              <div class="manual-provider-head">
                <strong>{{ provider.name }}</strong>
              </div>
              <div class="manual-keywords">
                <a
                  v-for="link in provider.links"
                  :key="`${provider.provider}-${link.keyword}`"
                  :href="link.url"
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {{ link.keyword }}
                </a>
              </div>
            </div>
          </aside>
        </div>
      </VCardText>
      <VDivider />
      <VCardActions class="online-download-actions">
        <span class="online-selection-count">已选 {{ selectedOnlineResults.length }} 条</span>
        <VSpacer />
        <VBtn
          color="success"
          :disabled="!selectedOnlineResults.length || onlineAiDownloading"
          :loading="onlinePreviewDownloading"
          @click="$emit('download-online-preview')"
        >
          下载并生成预览
        </VBtn>
        <VBtn
          color="primary"
          variant="tonal"
          :disabled="!canSubmitOnlineAiTranslate || onlinePreviewDownloading"
          :loading="onlineAiDownloading"
          @click="$emit('request-online-ai-translate')"
        >
          提交 AI 翻译
        </VBtn>
        <VBtn
          v-if="onlineDownloading"
          color="warning"
          variant="tonal"
          @click="$emit('stop-online-download')"
        >
          停止等待
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>

  <VDialog
    :model-value="onlineAiConfirmDialog"
    max-width="520"
    @update:model-value="$emit('update:onlineAiConfirmDialog', $event)"
  >
    <VCard rounded="lg">
      <VCardTitle class="dialog-title compact">
        <div>
          <span>确认提交 AI 翻译</span>
          <p>{{ onlineAiConfirmText }}</p>
        </div>
      </VCardTitle>
      <VDivider />
      <VCardText>
        <VAlert
          type="warning"
          variant="tonal"
          text="确认后会在后台下载所选外语字幕，智能调轴后提交到 AI 字幕生成队列；不会打开匹配预览，误触后可在 AI 状态里取消。"
        />
      </VCardText>
      <VCardActions class="justify-end">
        <VBtn variant="text" @click="$emit('update:onlineAiConfirmDialog', false)">取消</VBtn>
        <VBtn
          color="primary"
          variant="flat"
          :loading="onlineAiDownloading"
          @click="$emit('confirm-online-ai-translate')"
        >
          确认提交
        </VBtn>
      </VCardActions>
    </VCard>
  </VDialog>
</template>

<style scoped>
.online-dialog {
  background: var(--smu-dialog-bg);
  color: var(--smu-text);
  backdrop-filter: blur(16px);
}

.dialog-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.dialog-title p {
  margin: 4px 0 0;
  color: var(--smu-text-muted);
  font-size: 12px;
  font-weight: 400;
}

.online-scroll-body {
  min-height: 0;
  overflow-y: auto;
}

.online-download-actions {
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 16px max(12px, env(safe-area-inset-bottom));
}

.online-selection-count {
  color: var(--smu-text-muted);
  white-space: nowrap;
}

.mobile-online-dialog-title {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  align-items: start;
}

.mobile-online-dialog-title .online-dialog-heading {
  min-width: 0;
}

.online-search-actions {
  display: grid;
  grid-template-columns: minmax(220px, 1fr) minmax(220px, 0.7fr) auto;
  gap: 12px;
  padding: 14px 18px;
  background: var(--smu-dialog-bar-bg);
}

.online-message-summary {
  margin-bottom: 14px;
}

.online-message-summary-content {
  display: flex;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
}

.online-message-summary-content span {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.online-layout {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 300px;
  gap: 16px;
}

.online-results-panel,
.manual-links-panel {
  align-self: start;
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--smu-border);
  border-radius: 22px;
  background: var(--smu-card-bg);
}

.online-panel-head {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  align-items: center;
  margin-bottom: 12px;
}

.online-panel-head h3,
.manual-links-panel h3 {
  margin: 4px 0 0;
}

.online-panel-head span,
.manual-links-panel p,
.manual-provider-head span,
.online-result-meta,
.online-result-main p {
  color: var(--smu-text-muted);
  font-size: 12px;
}

.section-kicker {
  color: var(--smu-accent);
  font-size: 12px;
  font-weight: 900;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.online-provider-filter {
  margin: -4px 0 12px;
}

.online-filter-hint {
  margin: 0 0 12px;
  color: var(--smu-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.online-provider-progress {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}

.online-provider-filter-active {
  background: var(--smu-accent) !important;
  color: var(--smu-accent-text) !important;
}

.online-loading {
  padding: 24px;
  border-radius: 18px;
  background: var(--smu-card-bg-soft);
  color: var(--smu-text-muted);
  text-align: center;
}

.online-result-list {
  display: grid;
  gap: 10px;
}

.online-result-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  gap: 10px;
  align-items: center;
  padding: 12px;
  border: 1px solid var(--smu-border);
  border-radius: 18px;
  background: var(--smu-card-bg);
}

.online-result-card.active {
  border-color: var(--smu-border-active);
  background: var(--smu-card-bg-active);
}

.online-result-card.disabled {
  opacity: 0.72;
  background: var(--smu-card-bg-disabled);
}

.online-result-main {
  min-width: 0;
}

.online-result-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}

.online-listing-metadata {
  color: var(--smu-text-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.online-result-title {
  font-weight: 900;
  word-break: break-word;
}

.online-result-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}

.online-manual-badge {
  padding: 1px 8px;
  border: 1px solid var(--smu-border-active);
  border-radius: 999px;
  background: var(--smu-accent-soft);
  color: var(--smu-accent);
  font-weight: 800;
}

.online-result-main p {
  margin: 6px 0 0;
}

.online-match-detail {
  color: var(--smu-accent) !important;
}

.online-open-link,
.manual-keywords a {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 28px;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--smu-accent-soft);
  color: var(--smu-accent);
  font-size: 12px;
  font-weight: 900;
  text-decoration: none;
}

.manual-links-panel p {
  margin: 8px 0 14px;
  line-height: 1.6;
}

.manual-provider {
  display: grid;
  gap: 8px;
  padding: 10px 0;
  border-top: 1px solid var(--smu-border);
}

.manual-provider-head {
  display: grid;
  gap: 2px;
}

.manual-keywords {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.empty-state {
  padding: 28px 18px;
  border-radius: 22px;
  background: var(--smu-card-bg-soft);
  color: var(--smu-text-muted);
  text-align: center;
}

@media (max-width: 900px) {
  .online-search-actions,
  .online-layout {
    grid-template-columns: 1fr;
  }

}

@media (max-width: 600px) {
  .online-result-card {
    grid-template-columns: auto minmax(0, 1fr);
  }

  .online-result-actions {
    grid-column: 2;
  }
}
</style>
