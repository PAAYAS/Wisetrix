import { ProjectNav } from "@/components/project-nav";

export default function ProjectLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: { id: string };
}) {
  return (
    <>
      <ProjectNav projectId={params.id} />
      {children}
    </>
  );
}
