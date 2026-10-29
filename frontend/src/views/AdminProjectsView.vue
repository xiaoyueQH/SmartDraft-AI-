<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from "vue";
import { useRouter, onBeforeRouteLeave } from "vue-router";
import { auth } from "../auth/session";
import {
  kindLabels,
  stateLabels,
  itemStateLabel,
  supportReasonLabels,
  supportNextStep,
  type SupportInbox,
  type SupportReason,
  type SupportRequest,
  type Project,
  type ProjectItem,
} from "../api/projects";
import RecordManager from "../components/RecordManager.vue";
import NewConsultation from "../components/NewConsultation.vue";

type Section =
  "overview" | "projects" | "reviews" | "tasks" | "deliveries" | "service";
type ManagedProject = Project & {
  customer_name: string;
  customer_username: string;
};
type ManagedItem = ProjectItem & {
  project_title: string;
  customer_name: string;
  customer_username: string;
};
const props = withDefaults(
  defineProps<{ section?: Section; initialPhase?: string }>(),
  { section: "overview", initialPhase: "" },
);
const router = useRouter();
const settings = {
  overview: {
    title: "管理工作台",
    description: "查看客户接待、需求审核与项目交付的整体情况。",
    category: "",
  },
  projects: {
    title: "客户与项目",
    description: "按客户查找沟通记录与正式项目，管理完整业务档案。",
    category: "",
  },
  reviews: {
    title: "需求审核",
    description: "审核初版需求与需求变更，核对待确认问题和双方确认状态。",
    category: "documents",
  },
  tasks: {
    title: "开发任务",
    description: "集中管理各项目的任务、处理结果与客户验收。",
    category: "tasks",
  },
  deliveries: {
    title: "交付验收",
    description: "跟进交付材料、验收反馈及退回处理。",
    category: "deliveries",
  },
  service: {
    title: "售后工单",
    description: "处理故障反馈与使用咨询，保留沟通与处理依据。",
    category: "service",
  },
};
const config = computed(() => settings[props.section]),
  isItems = computed(() => !!config.value.category);
const rows = ref<(ManagedProject | ManagedItem)[]>([]),
  summary = ref<Record<string, number>>({}),
  offset = ref(0),
  more = ref(false),
  loading = ref(false),
  notice = ref(""),
  query = ref(""),
  appliedQuery = ref(""),
  phase = ref(""),
  status = ref("active"),
  recordBusy = ref(false);
const manager = ref<InstanceType<typeof RecordManager> | null>(null),
  creator = ref<InstanceType<typeof NewConsultation> | null>(null);
const controller = new AbortController();
const supportRows = ref<SupportRequest[]>([]),
  supportCounts = ref<Partial<SupportInbox["counts"]>>({}),
  supportReason = ref<SupportReason | "">(""),
  supportQuery = ref(""),
  appliedSupportQuery = ref(""),
  supportOffset = ref(0),
  supportMore = ref(false),
  supportLoading = ref(false),
  supportNotice = ref(""),
  supportCheckedAt = ref("");
let supportGeneration = 0,
  supportTimer: ReturnType<typeof setTimeout> | null = null;
let generation = 0,
  disposed = false;
const cards = [
  {
    key: "consultations",
    label: "需求沟通中",
    path: "/admin/projects",
    phase: "discovery",
  },
  {
    key: "projects",
    label: "正式项目",
    path: "/admin/projects",
    phase: "project",
  },
  {
    key: "pending_reviews",
    label: "待负责人审核",
    path: "/admin/reviews",
    phase: "",
  },
  {
    key: "active_tasks",
    label: "进行中的任务",
    path: "/admin/tasks",
    phase: "",
  },
  {
    key: "active_deliveries",
    label: "交付与验收",
    path: "/admin/deliveries",
    phase: "",
  },
  {
    key: "open_service",
    label: "未结束工单",
    path: "/admin/service",
    phase: "",
  },
];
async function load(page = 0) {
  const current = ++generation;
  loading.value = true;
  notice.value = "";
  const params = new URLSearchParams({
    offset: String(page),
    limit: "20",
    q: appliedQuery.value,
  });
  if (isItems.value) {
    params.set("category", config.value.category);
    params.set("status", status.value);
  } else params.set("phase", phase.value);
  try {
    const [result, counts] = await Promise.all([
      auth.request<{
        items: (ManagedProject | ManagedItem)[];
        has_more: boolean;
      }>(`/api/admin/projects${isItems.value ? "/items" : ""}?${params}`, {
        signal: controller.signal,
      }),
      props.section === "overview"
        ? auth.request<Record<string, number>>("/api/admin/projects/summary", {
            signal: controller.signal,
          })
        : Promise.resolve(null),
    ]);
    if (disposed || current !== generation) return;
    rows.value = result.items;
    more.value = result.has_more;
    offset.value = page;
    if (counts) summary.value = counts;
    if (!rows.value.length && page > 0) await load(0);
  } catch (error) {
    if (!disposed && current === generation) {
      rows.value = [];
      more.value = false;
      notice.value =
        error instanceof Error ? error.message : "读取失败，请稍后重试";
    }
  } finally {
    if (!disposed && current === generation) loading.value = false;
  }
}
async function loadSupport(page = 0) {
  if (supportTimer) clearTimeout(supportTimer);
  const current = ++supportGeneration;
  supportLoading.value = true;
  supportNotice.value = "";
  const params = new URLSearchParams({
    scope: "all",
    reason: supportReason.value,
    q: appliedSupportQuery.value,
    offset: String(page),
    limit: "10",
  });
  try {
    const result = await auth.request<SupportInbox>(
      `/api/conversations/requests?${params}`,
      { signal: controller.signal },
    );
    if (
      disposed ||
      current !== supportGeneration ||
      props.section !== "overview"
    )
      return;
    supportRows.value = result.items;
    supportCounts.value = result.counts ?? {};
    supportMore.value = result.has_more;
    supportOffset.value = page;
    supportCheckedAt.value = result.checked_at;
    if (!result.items.length && page > 0) await loadSupport(0);
  } catch (error) {
    if (!disposed && current === supportGeneration) {
      supportRows.value = [];
      supportCounts.value = {};
      supportMore.value = false;
      supportNotice.value =
        error instanceof Error ? error.message : "接待列表读取失败，请重新查询";
    }
  } finally {
    if (
      !disposed &&
      current === supportGeneration &&
      props.section === "overview"
    ) {
      supportLoading.value = false;
      supportTimer = setTimeout(
        () => void loadSupport(supportOffset.value),
        15000,
      );
    }
  }
}
function searchSupport() {
  appliedSupportQuery.value = supportQuery.value.trim();
  void loadSupport();
}
function supportTime(value: string | null) {
  return value ? new Date(value).toLocaleString() : "—";
}
function search() {
  appliedQuery.value = query.value.trim();
  void load();
}
function openRow(row: ManagedProject | ManagedItem) {
  if (loading.value) return;
  const item = "project_id" in row;
  void router.push({
    path: `/admin/projects/${item ? row.project_id : row.id}`,
    query: item
      ? {
          tab:
            config.value.category === "deliveries"
              ? "acceptance"
              : config.value.category === "service"
                ? "service"
                : config.value.category === "documents"
                  ? "documents"
                  : "tasks",
          item: row.id,
        }
      : {},
  });
}
function created(project: Project) {
  void router.push(`/admin/projects/${project.id}`);
}
watch(
  () => [props.section, props.initialPhase],
  () => {
    query.value = "";
    appliedQuery.value = "";
    phase.value = props.initialPhase;
    status.value = props.section === "reviews" ? "pending_review" : "active";
    supportGeneration++;
    if (supportTimer) clearTimeout(supportTimer);
    supportTimer = null;
    if (props.section === "overview") void loadSupport();
    void load();
  },
  { immediate: true },
);
onBeforeRouteLeave(() => !(recordBusy.value || creator.value?.busy()));
onUnmounted(() => {
  disposed = true;
  generation++;
  supportGeneration++;
  if (supportTimer) clearTimeout(supportTimer);
  controller.abort();
});
</script>

<template>
  <section class="admin-projects" :aria-busy="loading">
    <div class="page-heading">
      <div>
        <h1>{{ config.title }}</h1>
        <p>{{ config.description }}</p>
      </div>
      <div class="workspace-header-actions">
        <button
          class="button button-outline"
          :disabled="recordBusy"
          @click="manager?.open()"
        >
          记录与回收站</button
        ><button class="button button-dark" @click="creator?.open()">
          发起客户沟通
        </button>
      </div>
    </div>
    <p v-if="notice" class="notice notice-warning" role="alert">
      {{ notice }}
      <button class="text-button" @click="load(offset)">重新读取</button>
    </p>
    <div v-if="section === 'overview'" class="management-stats">
      <button
        v-for="card in cards"
        :key="card.key"
        @click="
          router.push({
            path: card.path,
            query: card.phase ? { phase: card.phase } : {},
          })
        "
      >
        <span>{{ card.label }}</span
        ><strong>{{ loading ? "—" : (summary[card.key] ?? "—") }}</strong
        ><small>查看列表 →</small>
      </button>
    </div>
    <section
      v-if="section === 'overview'"
      class="management-table-panel support-inbox"
      aria-label="待处理接待"
      :aria-busy="supportLoading"
    >
      <div class="support-heading">
        <h2>待处理接待</h2>
        <span>全部原因共 {{ supportCounts.total ?? "—" }} 条 · 每15秒更新</span>
      </div>
      <p class="support-description">
        查看等待原因、接待人和到期时间。同一项目可有多个原因；客户只等人工时不会自动恢复AI。
      </p>
      <form class="management-filters" @submit.prevent="searchSupport">
        <label
          >客户或项目<input
            v-model="supportQuery"
            maxlength="80"
            placeholder="搜索客户或项目名称"
        /></label>
        <label
          >待处理原因<select v-model="supportReason">
            <option value="">全部原因</option>
            <option
              v-for="(label, reason) in supportReasonLabels"
              :key="reason"
              :value="reason"
            >
              {{ label }}（{{ supportCounts[reason] ?? "—" }}）
            </option>
          </select></label
        >
        <button class="button button-outline" :disabled="supportLoading">
          查询接待
        </button>
      </form>
      <p v-if="supportNotice" class="notice notice-warning" role="alert">
        {{ supportNotice
        }}<button class="text-button" @click="loadSupport(supportOffset)">
          重新读取接待
        </button>
      </p>
      <div class="table-scroll">
        <table class="management-table">
          <thead>
            <tr>
              <th>客户 / 项目</th>
              <th>等待原因</th>
              <th>接待人</th>
              <th>到期时间</th>
              <th>处理建议</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in supportRows" :key="item.id">
              <td>
                <strong>{{ item.customer_name }}</strong
                ><small>{{ item.title }}</small>
              </td>
              <td>
                <span
                  v-for="reason in item.reasons"
                  :key="reason"
                  class="support-reason"
                  :class="{ urgent: ['failed', 'expiring'].includes(reason) }"
                  >{{ supportReasonLabels[reason] }}</span
                ><small v-if="item.failure_summary">{{
                  item.failure_summary
                }}</small>
              </td>
              <td>{{ item.support_name || "尚未领取" }}</td>
              <td>
                {{
                  item.human_only
                    ? "只等人工，不自动切换"
                    : item.conversation_mode === "pending"
                      ? "本轮处理结束后开始计时"
                      : supportTime(item.deadline_at)
                }}
              </td>
              <td>{{ supportNextStep(item) }}</td>
              <td>
                <button
                  class="text-button"
                  :disabled="supportLoading"
                  @click="router.push(`/admin/projects/${item.id}`)"
                >
                  查看并处理
                </button>
              </td>
            </tr>
            <tr v-if="!supportRows.length && !supportNotice">
              <td colspan="6">
                {{
                  supportLoading ? "正在读取接待…" : "当前条件下暂无待处理接待"
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          class="button button-outline"
          :disabled="supportLoading || supportOffset === 0"
          @click="loadSupport(supportOffset - 10)"
        >
          接待上一页</button
        ><button
          class="button button-outline"
          :disabled="supportLoading || !supportMore"
          @click="loadSupport(supportOffset + 10)"
        >
          接待下一页</button
        ><small v-if="supportCheckedAt"
          >更新于 {{ supportTime(supportCheckedAt) }}</small
        >
      </div>
    </section>
    <section class="management-table-panel">
      <h2>
        {{ section === "overview" ? "最近客户记录" : config.title + "列表" }}
      </h2>
      <form class="management-filters" @submit.prevent="search">
        <label
          >搜索<input
            v-model="query"
            maxlength="80"
            placeholder="项目、客户或记录名称"
            :disabled="loading" /></label
        ><label v-if="!isItems"
          >阶段<select v-model="phase" :disabled="loading">
            <option value="">全部阶段</option>
            <option value="discovery">需求沟通中</option>
            <option value="project">正式项目</option>
          </select></label
        ><label v-else
          >状态<select v-model="status" :disabled="loading">
            <option v-if="section === 'reviews'" value="pending_review">
              待负责人审核
            </option>
            <option value="active">未结束</option>
            <option value="all">全部状态</option>
            <option
              v-for="(label, value) in stateLabels"
              :key="value"
              :value="value"
            >
              {{ label }}
            </option>
          </select></label
        ><button class="button button-outline" :disabled="loading">查询</button>
      </form>
      <div class="table-scroll">
        <table class="accounts-table">
          <thead>
            <tr>
              <th>{{ isItems ? "业务记录 / 项目" : "沟通记录 / 项目" }}</th>
              <th>客户</th>
              <th>{{ isItems ? "类型 / 状态" : "阶段 / 基线" }}</th>
              <th>创建时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id">
              <td>
                <strong>{{ row.title }}</strong
                ><small>{{
                  "project_title" in row ? row.project_title : row.id.slice(-8)
                }}</small>
              </td>
              <td>
                {{ row.customer_name
                }}<small>{{ row.customer_username }}</small>
              </td>
              <td v-if="'kind' in row">
                <span
                  >{{ kindLabels[row.kind] }} · {{ itemStateLabel(row) }}</span
                ><small v-if="['specification', 'change'].includes(row.kind)"
                  >负责人{{ row.staff_confirmed ? "已确认" : "未确认" }} ·
                  客户{{ row.customer_confirmed ? "已确认" : "未确认" }}</small
                >
              </td>
              <td v-else>
                {{
                  row.phase === "discovery"
                    ? "需求沟通中"
                    : `正式项目 · V${row.revision}`
                }}
              </td>
              <td>{{ new Date(row.created_at).toLocaleString("zh-CN") }}</td>
              <td>
                <button
                  class="text-button"
                  :disabled="loading"
                  @click="openRow(row)"
                >
                  {{
                    section === "reviews"
                      ? "查看并审核"
                      : isItems
                        ? "查看并处理"
                        : "管理项目"
                  }}
                </button>
              </td>
            </tr>
            <tr v-if="!rows.length">
              <td colspan="5">
                {{ loading ? "正在读取…" : "暂无符合条件的记录" }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="pagination">
        <button
          class="button button-outline"
          :disabled="loading || offset === 0"
          @click="load(Math.max(0, offset - 20))"
        >
          上一页</button
        ><button
          class="button button-outline"
          :disabled="loading || !more"
          @click="load(offset + 20)"
        >
          下一页
        </button>
      </div>
    </section>
    <RecordManager
      ref="manager"
      :admin="true"
      @busy="recordBusy = $event"
      @changed="load(offset)"
    />
    <NewConsultation ref="creator" @created="created" />
  </section>
</template>

<style scoped>
.support-inbox {
  margin-bottom: 24px;
}
.support-heading {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  align-items: baseline;
  flex-wrap: wrap;
}
.support-heading span,
.support-description,
.pagination small {
  color: #64748b;
  font-size: 13px;
}
.support-reason {
  display: inline-block;
  margin: 3px 5px 3px 0;
  padding: 3px 7px;
  border-radius: 4px;
  background: #edf3fa;
  color: #365b87;
  font-size: 12px;
}
.support-reason.urgent {
  background: #fff2dc;
  color: #86540d;
}
.support-inbox td {
  min-width: 110px;
}
.support-inbox td:first-child,
.support-inbox td:nth-child(5) {
  min-width: 180px;
}
.support-inbox td small {
  display: block;
  margin-top: 5px;
}
</style>
