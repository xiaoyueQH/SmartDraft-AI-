<script setup lang="ts">
import { computed } from "vue";
import type { AgentRun } from "../api/projects";

const props = defineProps<{ run: AgentRun }>();
const labels: Record<string, string> = {
  model: "模型",
  tool: "工具",
  skill: "Skill",
  mcp: "MCP",
  saved: "保存",
};
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
          ? "正在思考"
          : run.status === "failed"
            ? "执行未完成"
            : "已完成"
      }}</span>
      <small v-if="calls">{{ calls }} 次调用</small>
      <span class="execution-chevron" aria-hidden="true">⌄</span>
    </summary>
    <div class="execution-content">
      <p v-if="!events.length" class="project-muted">正在等待模型响应…</p>
      <ol v-else aria-label="本轮执行步骤">
        <li
          v-for="event in events"
          :key="event.sequence"
          :class="{ 'execution-failed': event.ok === false }"
        >
          <div class="execution-step-heading">
            <span class="execution-kind">{{ labels[event.type] }}</span
            ><strong v-if="event.tool">{{ event.tool }}</strong
            ><small v-if="event.ok === false">未成功</small>
          </div>
          <p>{{ event.summary }}</p>
          <small v-if="event.duration_ms !== undefined"
            >{{ event.duration_ms }} ms<span v-if="event.tokens?.total_tokens">
              · {{ event.tokens.total_tokens }} tokens</span
            ></small
          >
        </li>
      </ol>
    </div>
  </details>
</template>
