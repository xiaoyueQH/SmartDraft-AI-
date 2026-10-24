<script setup lang="ts">
import { RouterLink } from "vue-router";

const services = [
  {
    number: "01",
    title: "了解商品",
    text: "说明、使用方式和常见问题，找到有依据的解答。",
  },
  {
    number: "02",
    title: "跟进订单",
    text: "从订单到物流，让每一步进展都清楚可见。",
  },
  {
    number: "03",
    title: "处理售后",
    text: "退换申请、进度跟踪，接着上一次的沟通继续。",
  },
];
</script>

<template>
  <section class="home-hero">
    <div class="hero-copy">
      <p class="eyebrow"><span></span>YOUR SERVICE, CONNECTED</p>
      <h1>从一个问题开始，<br />把事情<span>处理好。</span></h1>
      <p class="hero-description">
        有问题可以问，有进度可以查。<br />让商品、订单与售后，在同一个服务空间里衔接。
      </p>
      <div class="preview-notice">
        <span class="dot"></span>服务空间搭建中，商品与售后功能暂未开放。
      </div>
      <RouterLink class="button button-dark" to="/status"
        >查看服务状态 <span aria-hidden="true">↗</span></RouterLink
      >
    </div>
    <div class="hero-art" aria-hidden="true">
      <div class="orbit orbit-one"></div>
      <div class="orbit orbit-two"></div>
      <div class="art-label">LESS WAITING.<br />MORE RESOLVING.</div>
      <div class="art-tile tile-question">
        <span>?</span><small>听见问题</small>
      </div>
      <div class="art-tile tile-connect">
        <span>↗</span><small>连接服务</small>
      </div>
      <div class="art-tile tile-done">
        <span>✓</span><small>跟进结果</small>
      </div>
      <span class="art-caption">一个问题，一段有始有终的服务。</span>
    </div>
  </section>
  <section class="service-section" aria-labelledby="service-title">
    <div class="section-heading">
      <h2 id="service-title">围绕你的每一次需要</h2>
      <span>即将提供</span>
    </div>
    <div class="service-grid">
      <article
        v-for="service in services"
        :key="service.number"
        class="service-card"
      >
        <span class="card-number">{{ service.number }}</span>
        <h3>{{ service.title }}</h3>
        <p>{{ service.text }}</p>
      </article>
    </div>
  </section>
</template>
