import { ChainService } from "./chain.ts";
import { ConfigError, loadConfig } from "./config.ts";
import { createLogger } from "./logger.ts";
import { ConnectionRpc, guardDevnet } from "./rpc.ts";
import { createServer } from "./server.ts";
import { DepositService } from "./deposit.ts";
import { WalletService } from "./wallet.ts";

async function main() {
  const cfg = loadConfig();
  const logger = createLogger({ level: cfg.logLevel });
  const rpc = ConnectionRpc.create(cfg.rpcUrl, logger);
  await guardDevnet(rpc); // aborts the process unless the RPC is devnet
  const chain = new ChainService({ rpc, cfg, logger });
  const deposits = new DepositService({ rpc, cfg, chain, logger });
  const wallets = new WalletService({ rpc, cfg, chain, logger });
  const server = createServer({ chain, secret: cfg.secret, logger, deposits, wallets });
  server.listen(cfg.port, () =>
    logger.info("chain_service.started", { port: cfg.port, program: cfg.programId.toBase58(), payer: cfg.payer.publicKey.toBase58(), oracle: cfg.oracle.publicKey.toBase58() }),
  );
}

main().catch((e) => {
  console.error(e instanceof ConfigError ? `Config error: ${e.message}` : `Startup failed: ${e instanceof Error ? e.message : String(e)}`);
  process.exit(1);
});
