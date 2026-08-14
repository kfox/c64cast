<script lang="ts">
  import type { Snippet } from "svelte";

  interface Props {
    variant?: "primary" | "default" | "danger";
    disabled?: boolean;
    title?: string;
    type?: "button" | "submit";
    onclick?: () => void;
    children: Snippet;
  }

  let {
    variant = "default",
    disabled = false,
    title,
    type = "button",
    onclick,
    children,
  }: Props = $props();

  const variants: Record<NonNullable<Props["variant"]>, string> = {
    primary: "bg-[var(--accent)] text-[var(--accent-ink)] border-transparent",
    default: "bg-[var(--panel-alt)] text-[var(--ink)] border-[var(--edge)]",
    danger: "bg-transparent text-c64-red border-c64-red/60",
  };
</script>

<button
  {type}
  {title}
  {disabled}
  {onclick}
  class="inline-flex min-h-11 items-center justify-center gap-2 rounded-lg border
         px-4 text-sm font-medium transition
         enabled:hover:brightness-110 enabled:active:brightness-95
         disabled:cursor-not-allowed disabled:opacity-40
         focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]
         {variants[variant]}"
>
  {@render children()}
</button>
