import { getChatGPTUser } from "./chatgpt-auth";
import { OpsConsole } from "./OpsConsole";

export const dynamic = "force-dynamic";

export default async function Home() {
  const user = await getChatGPTUser();
  return <OpsConsole user={user ? { name: user.displayName, email: user.email } : null} />;
}
