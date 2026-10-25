import { createRouter, createWebHashHistory } from "vue-router";

import AdminLayout from "../layouts/AdminLayout.vue";
import CustomerLayout from "../layouts/CustomerLayout.vue";
import ProjectsView from "../views/ProjectsView.vue";
import AdminProjectsView from "../views/AdminProjectsView.vue";
import ToolSettingsView from "../views/ToolSettingsView.vue";
import NotFoundView from "../views/NotFoundView.vue";
import StatusView from "../views/StatusView.vue";
import LoginView from "../views/LoginView.vue";
import UsersView from "../views/UsersView.vue";
import AccountView from "../views/AccountView.vue";
import { auth } from "../auth/session";

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: "/login", component: LoginView, meta: { title: "登录" } },
    {
      path: "/",
      component: CustomerLayout,
      meta: { requiresAuth: true },
      children: [
        { path: "", component: ProjectsView, meta: { title: "需求沟通" } },
        { path: "projects", redirect: "/" },
        { path: "status", component: StatusView, meta: { title: "服务状态" } },
        {
          path: "account",
          component: AccountView,
          meta: { title: "账号设置" },
        },
      ],
    },
    {
      path: "/admin",
      component: AdminLayout,
      meta: { requiresAuth: true, adminOnly: true },
      children: [
        {
          path: "",
          component: AdminProjectsView,
          props: { section: "overview" },
          meta: { title: "管理工作台" },
        },
        {
          path: "projects",
          component: AdminProjectsView,
          props: (route) => ({
            section: "projects",
            initialPhase: ["discovery", "project"].includes(
              String(route.query.phase),
            )
              ? route.query.phase
              : "",
          }),
          meta: { title: "客户与项目" },
        },
        {
          path: "reviews",
          component: AdminProjectsView,
          props: { section: "reviews" },
          meta: { title: "需求审核" },
        },
        {
          path: "tasks",
          component: AdminProjectsView,
          props: { section: "tasks" },
          meta: { title: "开发任务" },
        },
        {
          path: "deliveries",
          component: AdminProjectsView,
          props: { section: "deliveries" },
          meta: { title: "交付验收" },
        },
        {
          path: "service",
          component: AdminProjectsView,
          props: { section: "service" },
          meta: { title: "售后工单" },
        },
        {
          path: "projects/:projectId",
          component: ProjectsView,
          props: (route) => ({
            projectId: route.params.projectId,
            initialTab: route.query.tab,
            initialItemId: route.query.item,
          }),
          meta: { title: "项目详情" },
        },
        { path: "status", component: StatusView, meta: { title: "运行状态" } },
        {
          path: "tools",
          component: ToolSettingsView,
          meta: { title: "工具管理" },
        },
        { path: "users", component: UsersView, meta: { title: "账号管理" } },
      ],
    },
    {
      path: "/:pathMatch(.*)*",
      component: NotFoundView,
      meta: { title: "页面未找到" },
    },
  ],
  scrollBehavior: () => ({ top: 0 }),
});

router.beforeEach(async (to) => {
  if (!to.meta.requiresAuth) return true;
  try {
    const user = await auth.ensureSession();
    if (!user) return { path: "/login", query: { next: to.fullPath } };
    if (to.meta.adminOnly && user.role !== "admin") return "/";
    if (user.role === "admin" && to.path === "/") return "/admin";
    return true;
  } catch {
    return {
      path: "/login",
      query: { next: to.fullPath, reason: "connection" },
    };
  }
});

router.afterEach((to) => {
  document.title = `${String(to.meta.title || "服务工作台")} · SupportFlow`;
});
