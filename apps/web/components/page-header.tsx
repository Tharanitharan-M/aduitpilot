"use client"

import { usePathname } from "next/navigation"
import Link from "next/link"
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "@/components/ui/breadcrumb"
import { SidebarTrigger } from "@/components/ui/sidebar"
import { Separator } from "@/components/ui/separator"
import { ThemeToggle } from "@/components/theme-toggle"

const ROUTE_LABELS: Record<string, string> = {
  "/dashboard": "Overview",
  "/dashboard/integrations": "Integrations",
  "/dashboard/controls": "Controls",
  "/dashboard/policies": "Policies",
  "/dashboard/questionnaire": "Questionnaire",
  "/dashboard/mock-audit": "Mock readiness challenge",
  "/dashboard/drift": "Compliance drift",
  "/dashboard/scan-runs": "Scan runs",
  "/dashboard/actions": "Actions",
  "/dashboard/chat": "Chat",
}

interface PageHeaderProps {
  title: string
  description?: string
  breadcrumbExtra?: { label: string; href?: string }
}

export function PageHeader({ title, description, breadcrumbExtra }: PageHeaderProps) {
  const pathname = usePathname() ?? "/dashboard"

  const parentPath = Object.keys(ROUTE_LABELS)
    .filter((r) => r !== "/dashboard")
    .find((r) => pathname.startsWith(r))

  const crumbs: { label: string; href?: string }[] = [
    { label: "Overview", href: "/dashboard" },
  ]

  if (parentPath && parentPath !== "/dashboard") {
    crumbs.push({
      label: ROUTE_LABELS[parentPath],
      href: parentPath,
    })
  }

  if (breadcrumbExtra) {
    crumbs.push(breadcrumbExtra)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mr-2 h-4" />
        <Breadcrumb className="min-w-0 flex-1">
          <BreadcrumbList className="flex-nowrap overflow-hidden">
            {crumbs.map((crumb, i) => {
              const isLast = i === crumbs.length - 1
              const key = `${i}-${crumb.href ?? crumb.label}`
              return (
                <span key={key} className="contents">
                  {i > 0 && <BreadcrumbSeparator />}
                  <BreadcrumbItem>
                    {isLast || !crumb.href ? (
                      <BreadcrumbPage className="truncate">{crumb.label}</BreadcrumbPage>
                    ) : (
                      <BreadcrumbLink render={<Link href={crumb.href} />}>
                        {crumb.label}
                      </BreadcrumbLink>
                    )}
                  </BreadcrumbItem>
                </span>
              )
            })}
          </BreadcrumbList>
        </Breadcrumb>
        <ThemeToggle className="ml-auto" />
      </div>

      <div>
        <h1 className="text-lg font-semibold tracking-tight md:text-xl">
          {title}
        </h1>
        {description && (
          <p className="mt-0.5 text-sm text-muted-foreground">{description}</p>
        )}
      </div>
    </div>
  )
}
