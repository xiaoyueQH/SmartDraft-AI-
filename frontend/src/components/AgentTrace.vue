<script setup lang="ts">
import { computed } from "vue";
import type { AgentEvent, AgentRun } from "../api/projects";

const props = defineProps<{ run: AgentRun }>();
const labels: Record<string, string> = {
  model: "模型调用",
  queue: "处理进度",
  tool: "工具调用",
  skill: "扩展功能",
  mcp: "外部服务",
  saved: "保存结果",
  node: "信息整理",
};
const tools: Record<string, string> = {
  read_project: "查看项目信息",
  read_document: "查看需求文档",
  remember_requirements: "整理业务信息",
  record_consultation: "记录本次业务信息",
  compare_baseline: "核对需求变更",
  save_draft: "整理需求草案",
  generate_prd: "需求生成：整理完整需求说明",
  write_prd_section: "展开功能详细说明",
  plan_draft: "组织需求书章节",
  read_draft: "查看已整理的正文",
  finish_draft: "汇总本次草案",
  web_search: "查询公开资料",
};
const steps: Record<string, string> = {
  extract: "整理已提供的业务信息",
  draft_sources: "读取完整沟通记录",
  draft_extract: "逐项整理业务要求",
  draft_write: "展开功能说明和验收方式",
  draft_plan: "组织需求书章节",
  draft_decide: "需求生成：安排后续章节",
  draft_section: "展开本章功能与验收方式",
  draft_review: "核对草案与客户要求",
  draft_complete: "整理完整草案",
  assess: "检查是否还有需要确认的内容",
  decide_draft: "判断是否可以整理草案",
  save_discussion: "保存本次沟通",
  memory_recovered: "已恢复原有沟通信息",
  draft_agent: "整理需求草案",
  research: "查找相关公开资料",
  draft: "整理需求草案",
  review: "检查草案是否符合您的要求",
  wait_user: "等待您继续补充",
  hitl: "沟通已保存，等待您继续",
  resume: "继续上次沟通",
};
function description(event: AgentEvent): string {
  const text =
    event.type === "queue"
      ? event.sequence === 1
        ? "请求已保存，等待处理"
        : "正在处理您的请求"
      : event.type === "model"
        ? "调用模型，整理您的需求"
        : event.type === "saved"
          ? "本轮沟通内容已保存"
          : event.type === "node"
            ? steps[event.node || ""] || "整理本次沟通信息"
            : tools[event.tool || ""] || "使用业务工具处理本次请求";
  return event.ok === false ? `${text}：暂未完成` : text;
}
const events = computed(() =>
  props.run.events.filter((event) => labels[event.type]),
);
const calls = computed(
  () =>
    events.value.filter((event) =>
      ["tool", "skill", "mcp"].includes(event.type),
    ).length,
);
</script>

<template>
  <details
    v-if="run.status === 'running' || events.length"
    class="chat-execution"
  >
    <summary>
      <span
        class="execution-indicator"
        :class="{ 'is-running': run.status === 'running' }"
        aria-hidden="true"
      ></span>
      <span>{{
        run.status === "running"
          ? events.length === 1 && events[0]?.type === "queue"
            ? "等待处理"
            : "正在思考"
          : run.status === "failed"
            ? "暂未完成"
            : events.some((event) => event.ok === false)
              ? "部分内容待完善"
              : "已完成"
      }}</span>
      <small v-if="calls">{{ calls }} 次调用</small>
      <span class="execution-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="execution-content">
      <p v-if="!events.length" class="project-muted">正在处理，请稍候…</p>
      <ol v-else aria-label="本轮执行步骤">
        <li
          v-for="event in events"
          :key="event.sequence"
          :class="{ 'execution-failed': event.ok === false }"
        >
          <div class="execution-step-heading">
            <span class="execution-kind">{{ labels[event.type] }}</span
            ><small v-if="event.ok === false">暂未完成</small>
          </div>
          <p>{{ description(event) }}</p>
          <small v-if="event.duration_ms !== undefined"
            >用时 {{ (event.duration_ms / 1000).toFixed(1) }} 秒</small
          >
        </li>
      </ol>
    </div>
  </details>
</template>
