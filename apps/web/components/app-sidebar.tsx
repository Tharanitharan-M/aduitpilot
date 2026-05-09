"use client"

import Link from "next/link"
import { usePathname } from "next/navigation"
import {
  LayoutDashboard,
  Plug,
  ShieldCheck,
  FileText,
  CheckSquare,
  MessageSquare,
  ClipboardList,
  Swords,
} from "lucide-react"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar"
import { SidebarUserMenu } from "@/components/sidebar-user-menu"

const NAV_ITEMS = [
  { label: "Overview", href: "/dashboard", icon: LayoutDashboard },
  { label: "Integrations", href: "/dashboard/integrations", icon: Plug },
  { label: "Controls", href: "/dashboard/controls", icon: ShieldCheck },
  { label: "Policies", href: "/dashboard/policies", icon: FileText },
  { label: "Questionnaire", href: "/dashboard/questionnaire", icon: ClipboardList },
  { label: "Mock Audit", href: "/dashboard/mock-audit", icon: Swords },
  { label: "Actions", href: "/dashboard/actions", icon: CheckSquare },
  { label: "Chat", href: "/dashboard/chat", icon: MessageSquare },
] as const

export function AppSidebar() {
  const pathname = usePathname() ?? "/dashboard"

  function isActive(href: string) {
    if (href === "/dashboard") return pathname === "/dashboard"
    return pathname.startsWith(href)
  }

  return (
    <Sidebar collapsible="icon" side="left">
      <SidebarHeader className="p-4 pb-2">
        <Link href="/dashboard" className="flex items-center gap-2.5">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-foreground text-background">
            <ShieldCheck className="h-4 w-4" />
          </div>
          <div className="flex flex-col group-data-[collapsible=icon]:hidden">
            <span className="text-sm font-semibold leading-none">AuditPilot</span>
            <span className="text-[10px] text-muted-foreground leading-tight mt-0.5">SOC 2 Readiness</span>
          </div>
        </Link>
      </SidebarHeader>

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              {NAV_ITEMS.map((item) => (
                <SidebarMenuItem key={item.href}>
                  <SidebarMenuButton
                    render={<Link href={item.href} />}
                    isActive={isActive(item.href)}
                    tooltip={item.label}
                  >
                    <item.icon className="h-4 w-4" />
                    <span>{item.label}</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
              ))}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      <SidebarFooter>
        <SidebarUserMenu />
      </SidebarFooter>
    </Sidebar>
  )
}
