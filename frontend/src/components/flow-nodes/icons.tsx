/** @jsxImportSource react */
// Flow-node icon resolver. **Real React** — see file-top pragma.
//
// The LLM picks an icon by kebab-case name (e.g. "git-branch") or
// passes a raw `{svg: "<svg .../>"}` inline. This module resolves
// either form into a renderable element.
//
// We import a curated subset of `lucide-preact` directly — the icons
// most useful for process / workflow / decision diagrams. The full
// library is ~1000 icons and ~1 MB if you import the index; cherry-
// picking like this is the standard pattern and keeps the bundle
// lean.
//
// Anything the LLM names that isn't in this map renders as a default
// type-specific icon (the caller passes a fallback). For arbitrary
// icons, use the `{svg: "..."}` form — the inline SVG is sanitised
// via DOMPurify and rendered verbatim.

import DOMPurify from "dompurify";
import {
  AlertOctagon,
  AlertTriangle,
  Archive,
  ArrowRight,
  Box,
  Calendar,
  Check,
  CheckCircle,
  CircleDot,
  Clock,
  Cloud,
  Database,
  DollarSign,
  Edit,
  Eye,
  File,
  FileText,
  Filter,
  Flag,
  Folder,
  Gauge,
  Gift,
  GitBranch,
  GitMerge,
  GitPullRequest,
  Hash,
  Hexagon,
  Inbox,
  Info,
  Key,
  Layers,
  Link,
  Lock,
  Mail,
  MessageCircle,
  Minus,
  Octagon,
  Package,
  Pause,
  Play,
  Plus,
  Power,
  RefreshCw,
  Repeat,
  RotateCcw,
  Save,
  Search,
  Send,
  Server,
  Settings,
  Settings2,
  Shield,
  ShieldAlert,
  ShieldCheck,
  Sigma,
  Square,
  Star,
  CircleStop,
  Tag,
  Target,
  Terminal,
  Trash,
  Triangle,
  TrendingUp,
  Truck,
  Upload,
  User,
  UserCheck,
  Users,
  Workflow,
  X,
  XCircle,
  Zap,
} from "lucide-react";

import type { FlowStep } from "../../lib/flow-types";

// The curated map. Keys are kebab-case lucide names (the icon catalog
// at https://lucide.dev/icons uses kebab-case in URLs). Values are
// the Preact components. Add new icons here as needs arise — they
// cost a few hundred bytes each.
const ICONS: Record<string, any> = {
  "alert-octagon": AlertOctagon,
  "alert-triangle": AlertTriangle,
  "archive": Archive,
  "arrow-right": ArrowRight,
  "box": Box,
  "calendar": Calendar,
  "check": Check,
  "check-circle": CheckCircle,
  "circle-dot": CircleDot,
  "clock": Clock,
  "cloud": Cloud,
  "database": Database,
  "dollar-sign": DollarSign,
  "edit": Edit,
  "eye": Eye,
  "file": File,
  "file-text": FileText,
  "filter": Filter,
  "flag": Flag,
  "folder": Folder,
  "gauge": Gauge,
  "gift": Gift,
  "git-branch": GitBranch,
  "git-merge": GitMerge,
  "git-pull-request": GitPullRequest,
  "hash": Hash,
  "hexagon": Hexagon,
  "inbox": Inbox,
  "info": Info,
  "key": Key,
  "layers": Layers,
  "link": Link,
  "lock": Lock,
  "mail": Mail,
  "message-circle": MessageCircle,
  "minus": Minus,
  "octagon": Octagon,
  "package": Package,
  "pause": Pause,
  "play": Play,
  "plus": Plus,
  "power": Power,
  "refresh-cw": RefreshCw,
  "repeat": Repeat,
  "rotate-ccw": RotateCcw,
  "save": Save,
  "search": Search,
  "send": Send,
  "server": Server,
  "settings": Settings,
  "settings-2": Settings2,
  "shield": Shield,
  "shield-alert": ShieldAlert,
  "shield-check": ShieldCheck,
  "sigma": Sigma,
  "square": Square,
  "star": Star,
  "stop": CircleStop,
  "circle-stop": CircleStop,
  "tag": Tag,
  "target": Target,
  "terminal": Terminal,
  "trash": Trash,
  "triangle": Triangle,
  "trending-up": TrendingUp,
  "truck": Truck,
  "upload": Upload,
  "user": User,
  "user-check": UserCheck,
  "users": Users,
  "workflow": Workflow,
  "x": X,
  "x-circle": XCircle,
  "zap": Zap,
};

// Type-default icons (used when a step has no explicit `icon`). Picks
// one that reads as schematic-friendly for each step type.
const TYPE_DEFAULTS: Record<FlowStep["type"], any> = {
  trigger: Play,
  activity: Square,
  decision: GitBranch,
  artifact: FileText,
  end: CircleDot,
};

interface IconProps {
  step: FlowStep;
  size?: number;
}

/** Render the icon for a step. Honours `step.icon` (lucide name or
 *  inline SVG), falls back to a type-specific default. */
export function StepIcon({ step, size = 14 }: IconProps) {
  const icon = step.icon;

  if (icon?.svg) {
    const safe = DOMPurify.sanitize(icon.svg, {
      USE_PROFILES: { svg: true, svgFilters: true },
    });
    return (
      <span
        className="flow-node__icon flow-node__icon--custom"
        dangerouslySetInnerHTML={{ __html: safe }}
      />
    );
  }

  if (icon?.name) {
    const Lucide = ICONS[icon.name];
    if (Lucide) {
      return <Lucide size={size} strokeWidth={1.75} />;
    }
    // Unknown icon name — fall through to type default rather than
    // silently rendering nothing. The LLM gets a "looks right" result
    // even with a misspelled icon name.
  }

  const Default = TYPE_DEFAULTS[step.type];
  return <Default size={size} strokeWidth={1.75} />;
}

/** Inline rendering of an arbitrary lucide name. Used for badge
 *  decorations etc. Falls back to nothing if name is unknown. */
export function NamedIcon({ name, size = 12 }: { name: string; size?: number }) {
  const Lucide = ICONS[name];
  if (!Lucide) return null;
  return <Lucide size={size} strokeWidth={2} />;
}

export const ICON_NAMES = Object.keys(ICONS);
