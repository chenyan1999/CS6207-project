import torch
import logging

from datasets import load_dataset
from utils import load_args, load_model, load_from_split_database, index_database, dataset_2_dataloader, clean
from trainer import train_RAG, val_RAG
from transformers import AdamW
from autoencoder import Autoencoder

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)

def main():
    args = load_args()
    logger.info(f"args: {args}")

    # load model
    tokenizer, model = load_model(args)
    model.config.question_encoder.max_position_embeddings = args.max_input_length
    optimizer = AdamW(model.parameters(), lr=args.learning_rate)
    
    # load database
    database = load_from_split_database(args.vec_database_path, args.init_database_name)
    # indexing the database
    corpus = database["text"]
    faissIndex = index_database(torch.tensor(database['embeddings']))
    
    # load dataset
    dataset_train = clean(load_dataset(args.dataset_name, split='train[:500]' if args.debug_model else 'train'))
    dataset_val = clean(load_dataset(args.dataset_name, split='validation[:500]' if args.debug_model else 'validation'))
    # dataset_test = load_dataset(args.dataset_name, split='test[:500]' if args.debug_model else 'test')
    logger.info(f"# Training samples: {len(dataset_train)}")
    logger.info(f"# Validation samples: {len(dataset_val)}")
    # logger.info(f"# Testing samples: {len(dataset_test)}")
    
    dataloader_train = dataset_2_dataloader(dataset_train, tokenizer, True, args)
    dataloader_val = dataset_2_dataloader(dataset_val, tokenizer, False, args)
    
    # load autoencoder
    autoencoder = Autoencoder(args.input_dim, args.latent_dim, args.device)
    for epoch in range(args.epoch_num):
        # 1. Train a RAG langauge model
        train_RAG(dataloader_train, model, tokenizer, optimizer, epoch, corpus, faissIndex, args)
        val_RAG(dataloader_val, model, tokenizer, epoch, corpus, faissIndex, args)
        
        # 2. Train an auto-encoder
        autoencoder.train_model(dataloader_train, dataloader_val, model, epoch, args)
        
        # 3. Update database
    
    
if __name__ == "__main__":
    main()