<script setup>
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  mobile: { type: Boolean, default: false },
  autoQueueSummaryText: { type: String, default: '' },
  autoTransferQueue: { type: Object, default: () => ({}) },
  autoQueueTasks: { type: Array, default: () => [] },
  autoQueueMutating: { type: Boolean, default: false },
  autoQueueActionTaskId: { type: String, default: '' },
  batchFollowupResult: { type: Object, default: null },
})

const skippedFollowupItems = computed(() => (props.batchFollowupResult?.items || []).filter(item => item.status !== 'queued'))
const followupReasons = {
  already_active: '已在队列',
  recent: '刚处理过',
  unavailable: '目标已失效或不可用',
  locked: '目标已锁定',
  read_only: '目标不可写',
  ineligible: '目标或当前设置不支持双语补搜',
  stopped: '自动入库服务已停止',
}

function queueTaskMessage(task) {
  const message = task.message || task.status
  return message === 'written' || message === 'completed' ? '已完成' : message
}

defineEmits([
  'update:modelValue',
  'load-auto-transfer-queue',
  'retry-auto-transfer-task',
  'force-auto-transfer-task',
  'cancel-bilingual-followup',
  'clear-auto-transfer-history',
])
</script>

<template>
  <VDialog
    :model-value="modelValue"
    :fullscreen="mobile"
    :scrollable="mobile"
    max-width="760"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <VCard
      class="auto-queue-card"
      :class="{ 'smu-mobile-dialog-card': mobile }"
      :rounded="mobile ? 0 : 'xl'"
    >
      <VCardTitle class="dialog-title">
        <div>
          <span>自动入库队列</span>
          <p>{{ autoQueueSummaryText }}</p>
        </div>
        <div class="online-title-actions">
          <VBtn
            v-if="autoQueueTasks.some(task => !['pending', 'in_progress'].includes(task.status))"
            variant="text"
            color="error"
            prepend-icon="mdi-delete-sweep-outline"
            :disabled="autoQueueMutating"
            @click="$emit('clear-auto-transfer-history')"
          >
            清空历史
          </VBtn>
          <VBtn
            variant="tonal"
            prepend-icon="mdi-refresh"
            :disabled="autoQueueMutating"
            @click="$emit('load-auto-transfer-queue')"
          >
            刷新
          </VBtn>
          <VBtn icon="mdi-close" variant="text" @click="$emit('update:modelValue', false)" />
        </div>
      </VCardTitle>
      <VDivider />
      <VCardText>
        <VAlert v-if="batchFollowupResult" type="info" variant="tonal" density="compact" class="mb-3">
          <span role="status">{{ batchFollowupResult.message }}</span>
          <details v-if="skippedFollowupItems.length">
            <summary>查看未加入的目标</summary>
            <ul class="ps-5">
              <li v-for="item in skippedFollowupItems" :key="item.target_id">
                {{ item.label }} · {{ followupReasons[item.status] || '未加入' }}
              </li>
            </ul>
          </details>
        </VAlert>
        <div class="auto-queue-rates">
          <span
            v-for="(rate, provider) in autoTransferQueue.rate_limits || {}"
            :key="provider"
          >
            {{ provider }}：{{ rate.remaining }}/{{ rate.limit_per_minute }} 可用
          </span>
        </div>
        <div v-if="autoQueueTasks.length" class="auto-queue-list">
          <div
            v-for="task in autoQueueTasks.slice().reverse()"
            :key="task.id"
            class="auto-queue-row"
            :class="`auto-queue-${task.status}`"
          >
            <div class="auto-queue-copy">
              <strong :title="task.target_label || task.title || task.id">{{ task.target_label || task.title || task.id }}</strong>
              <VTooltip location="top" max-width="520" :text="queueTaskMessage(task)">
                <template #activator="{ props: tooltipProps }">
                  <span v-bind="tooltipProps" class="auto-queue-message">{{ queueTaskMessage(task) }}</span>
                </template>
              </VTooltip>
              <small v-if="task.next_run_at">下次 {{ task.next_run_at }}</small>
            </div>
            <div v-if="task.can_retry" class="auto-queue-actions">
              <VBtn
                v-if="task.can_force_low_confidence"
                size="small"
                color="warning"
                variant="tonal"
                :loading="autoQueueActionTaskId === task.id"
                :disabled="autoQueueMutating && autoQueueActionTaskId !== task.id"
                @click="$emit('force-auto-transfer-task', task)"
              >
                强制入库
              </VBtn>
              <VBtn
                size="small"
                variant="tonal"
                :loading="autoQueueActionTaskId === task.id"
                :disabled="autoQueueMutating && autoQueueActionTaskId !== task.id"
                @click="$emit('retry-auto-transfer-task', task)"
              >
                {{ task.waiting_bilingual ? '立即补搜' : '重试' }}
              </VBtn>
              <VBtn
                v-if="task.waiting_bilingual"
                size="small"
                variant="text"
                :disabled="autoQueueMutating"
                @click="$emit('cancel-bilingual-followup', task)"
              >
                取消补搜
              </VBtn>
            </div>
          </div>
        </div>
        <div v-else class="empty-state compact-empty">
          当前没有自动入库任务。
        </div>
      </VCardText>
    </VCard>
  </VDialog>
</template>

<style scoped>
.auto-queue-card {
  margin-bottom: 14px;
  border: 1px solid var(--smu-border);
  background: var(--smu-card-bg-strong);
  color: var(--smu-text);
}

.dialog-title {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: flex-start;
}

.dialog-title p {
  margin: 4px 0 0;
  color: var(--smu-text-muted);
  font-size: 12px;
  font-weight: 400;
}

.online-title-actions,
.auto-queue-rates,
.auto-queue-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.online-title-actions {
  justify-content: flex-end;
}

.auto-queue-rates {
  justify-content: flex-start;
  flex-wrap: wrap;
  margin-top: 10px;
  color: var(--smu-text-muted);
  font-size: 0.82rem;
}

.auto-queue-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.auto-queue-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  border-radius: 14px;
  padding: 8px 10px;
  background: var(--smu-card-bg);
}

.auto-queue-copy {
  display: grid;
  min-width: 0;
  gap: 4px;
}

.auto-queue-copy strong {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.auto-queue-row span,
.auto-queue-row small {
  color: var(--smu-text-muted);
  font-size: 0.82rem;
}

.auto-queue-message {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.auto-queue-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}

.auto-queue-failed {
  border: 1px solid rgba(var(--v-theme-error), 0.30);
}

.auto-queue-in_progress,
.auto-queue-pending {
  border: 1px solid rgba(var(--v-theme-warning), 0.30);
}

.empty-state {
  padding: 28px 18px;
  border-radius: 22px;
  background: var(--smu-card-bg-soft);
  color: var(--smu-text-muted);
  text-align: center;
}

.compact-empty {
  padding: 16px;
  border-radius: 16px;
  background: var(--smu-card-bg-soft);
}

@media (max-width: 720px) {
  .dialog-title {
    display: grid;
  }

  .online-title-actions {
    width: 100%;
    justify-content: flex-end;
  }

  .auto-queue-row {
    grid-template-columns: minmax(0, 1fr);
  }

  .auto-queue-actions {
    justify-content: flex-start;
  }
}
</style>
