import { runtimeEnvironment, loadStrategyConfig } from "./config.js";
import { BinancePublicClient } from "./adapters/binance.js";
import { SupabaseRestRepository } from "./adapters/supabase.js";
import { GmailSmtpMailer } from "./adapters/smtp.js";

export function makeServerDependencies(): {
  config: ReturnType<typeof loadStrategyConfig>;
  environment: ReturnType<typeof runtimeEnvironment>;
  market: BinancePublicClient;
  repository: SupabaseRestRepository;
  mailer: GmailSmtpMailer;
} {
  const config = loadStrategyConfig();
  const environment = runtimeEnvironment();
  return {
    config,
    environment,
    market: new BinancePublicClient(environment.binanceBaseUrl),
    repository: new SupabaseRestRepository(environment.supabaseUrl, environment.supabaseServiceRoleKey),
    mailer: new GmailSmtpMailer(environment.gmailUser, environment.gmailAppPassword),
  };
}
