// Vite-specific module shims used in this project.

declare module "*.css?raw" {
  const css: string;
  export default css;
}
