<script setup lang="ts">
import { computed, ref, watch } from "vue";
import mermaid from "mermaid";
import { marked } from "marked";
import DOMPurify from "dompurify";

const props = defineProps<{ content: string }>();
const svg = ref("");
const documentHtml = computed(() =>
  DOMPurify.sanitize(
    marked.parse(
      props.content.replace(
        /```mermaid\s*\n[\s\S]*?```/g,
        "（业务流程图见上方）",
      ),
      { async: false },
    ),
    {
      ALLOWED_TAGS: [
        "p",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "strong",
        "em",
        "del",
        "ul",
        "ol",
        "li",
        "blockquote",
        "pre",
        "code",
        "table",
        "thead",
        "tbody",
        "tr",
        "th",
        "td",
        "hr",
        "a",
      ],
      ALLOWED_ATTR: ["href", "title", "colspan", "rowspan"],
    },
  ),
);
const source = computed(
  () => props.content.match(/```mermaid\s*\n([\s\S]*?)```/)?.[1] || "",
);
let generation = 0;
let renderId = 0;
mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  maxTextSize: 16000,
  flowchart: { htmlLabels: false },
  suppressErrorRendering: true,
});
watch(
  source,
  async (text) => {
    const current = ++generation;
    svg.value = "";
    if (!text || text.length > 16000) return;
    try {
      const result = await mermaid.render(
        `requirement-flow-${Date.now()}-${++renderId}`,
        text,
      );
      if (current === generation) svg.value = result.svg;
    } catch {
      /* The original diagram source remains available in the document. */
    }
  },
  { immediate: true },
);
</script>

<template>
  <div
    v-if="svg"
    class="requirement-flow"
    aria-label="业务流程图"
    v-html="svg"
  ></div>
  <div class="requirement-document" v-html="documentHtml"></div>
  <details v-if="source && !svg">
    <summary>流程图暂未渲染，查看原始描述</summary>
    <pre>{{ source }}</pre>
  </details>
</template>
