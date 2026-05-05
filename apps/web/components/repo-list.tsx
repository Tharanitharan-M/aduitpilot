/**
 * RepoList — renders connected GitHub repos as cards (chunk 3.7 / US-002).
 * Server Component — no interactivity needed here.
 */

import { Badge } from "@/components/ui/badge"
import { Card, CardContent } from "@/components/ui/card"

interface Repo {
  id: string
  full_name: string
  private: boolean
}

export function RepoList({ repos }: { repos: Repo[] }) {
  return (
    <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" aria-label="Repository list">
      {repos.map((repo) => (
        <li key={repo.id}>
          <Card>
            <CardContent className="flex items-center justify-between p-4">
              <span className="truncate text-sm font-medium">{repo.full_name}</span>
              <Badge variant={repo.private ? "secondary" : "outline"} className="ml-2 shrink-0">
                {repo.private ? "Private" : "Public"}
              </Badge>
            </CardContent>
          </Card>
        </li>
      ))}
    </ul>
  )
}
