import torch
import logging


from datasets import load_dataset
from utils import load_args, load_model, load_from_split_database, index_database, dataset_2_dataloader, clean, cluster_embeddings_with_faiss, cluster_embeddings_with_dbscan,cluster_embeddings_with_hierarchical, save_model
from trainer import train_RAG, test_RAG, val_RAG
from transformers import AdamW
from autoencoder import Autoencoder
import os
from utils import get_relevant_clusters
import tqdm
from transformers import AutoTokenizer, AutoModelForQuestionAnswering, pipeline

logging.basicConfig(format = '%(asctime)s - %(levelname)s - %(name)s - %(message)s',
                    datefmt = '%m/%d/%Y %H:%M:%S',
                    level = logging.INFO)
logger = logging.getLogger(__name__)

            

def test():
    args = load_args()
    logger.info(f"args: {args}")
    
    tokenizer = AutoTokenizer.from_pretrained("nbroad/deberta-v3-xsmall-squad2")
    model = AutoModelForQuestionAnswering.from_pretrained("nbroad/deberta-v3-xsmall-squad2")
    # load database
    database = load_from_split_database(args.vec_database_path, args.init_database_name)
    # indexing the database
    corpus = database["text"]
    embeddings = torch.tensor(database['embeddings']).numpy()
    if args.cluster == 'DBSCAN':
        cluster_centers, indexes,cluster_global_indices = cluster_embeddings_with_dbscan(embeddings, args.n_clusters)
    elif args.cluster == 'hierarchical':
        cluster_centers, indexes,cluster_global_indices = cluster_embeddings_with_hierarchical(embeddings, args.n_clusters)
    else:
        cluster_centers, indexes,cluster_global_indices = cluster_embeddings_with_faiss(embeddings, args.n_clusters)

    if args.debug_mode:
        dataset_train = clean(load_dataset(args.dataset_name, split='train[:10]' if args.debug_mode else 'train'))
        dataset_val = clean(load_dataset(args.dataset_name, split='validation[:10]' if args.debug_mode else 'validation'))
    else:
        # load dataset
        dataset_train = clean(load_dataset(args.dataset_name, split='train' if args.debug_mode else 'train'))
        dataset_val = clean(load_dataset(args.dataset_name, split='validation' if args.debug_mode else 'validation'))
        
    logger.info(f"# Validation samples: {len(dataset_train)}")
    #### from rag model load embeddings
    rag_model, rag_tokenizer, corpus, cluster_centers, indexes,cluster_global_indices = get_rag()
    dataloader_val, questions_text,answers_text = dataset_2_dataloader(dataset_train, rag_tokenizer, False, args)
    
    question_answerer = pipeline(
    "question-answering", model=model, tokenizer=tokenizer)
    
    outputs = []
    answers_texts = []
    for batch in dataloader_val:
        input_ids, attention_mask, labels,idx_tensor = batch
        input_ids = input_ids.to(args.device)
        attention_mask = attention_mask.to(args.device)
        question_hidden_states = rag_model.question_encoder(input_ids=input_ids,attention_mask=attention_mask)[0]
        context = retrieve(question_hidden_states, input_ids, rag_tokenizer, corpus, cluster_centers,indexes, cluster_global_indices,args)
        batch_questions_text = [questions_text[i.item()] for i in idx_tensor]
        batch_answers_text = [answers_text[i.item()] for i in idx_tensor]
        answers_texts.append(batch_answers_text)
        # print("context",context[0] )
        output = question_answerer(question=batch_questions_text, context=context[0])
        outputs.append(output)
    
    # EM between prediction & ground trut       
    golds = answers_texts
    print("outputs",len(outputs),outputs)
    em_count = 0
    for i, preds in enumerate(outputs):
        gold = golds[i][0]  # 假设每个问题只有一个“正确答案”
        if isinstance(preds, list):  # 处理返回多个答案的情况
            pred_texts = [pred['answer'] for pred in preds]
            # 如果任一预测答案在真实答案中，则认为是正确的
            if any(pred_text.strip() in gold for pred_text in pred_texts):
                em_count += 1
        elif isinstance(preds, dict):  # 处理返回单个答案的情况
            pred_text = preds['answer']
            if pred_text.strip() in gold:
                em_count += 1
    print(f"Exact Match: {em_count}/{len(output)}")

def get_rag(model_path='/home/yifan/projects/CS6207/CS6207-project/code/results/rag_model_DBSCAN_epoch_2.bin'):
    args = load_args()
    logger.info(f"args: {args}")
    # load model
    tokenizer, model = load_model(args)
    model.load_state_dict(torch.load(model_path))
    model.config.question_encoder.max_position_embeddings = args.max_input_length
    
    model.to(args.device)  # 将模型移动到选择的设备

    # optimizer = AdamW(model.parameters(), lr=args.learning_rate)
    
    # load database
    database = load_from_split_database(args.vec_database_path, args.init_database_name)
    # indexing the database
    corpus = database["text"]
    
    embeddings = torch.tensor(database['embeddings']).numpy()
    # Clustering embeddings
    
    if args.cluster == 'DBSCAN':
        cluster_centers, indexes,cluster_global_indices = cluster_embeddings_with_dbscan(embeddings, args.n_clusters)
    elif args.cluster == 'hierarchical':
        cluster_centers, indexes,cluster_global_indices = cluster_embeddings_with_hierarchical(embeddings, args.n_clusters)
    else:
        cluster_centers, indexes,cluster_global_indices = cluster_embeddings_with_faiss(embeddings, args.n_clusters)
    
    return model, tokenizer, corpus, cluster_centers, indexes,cluster_global_indices
    
    
    # bleu score between prediction & ground truth
     
def retrieve(query_embeddings, input_ids, tokenizer, corpus, cluster_centers, indexes, cluster_global_indices, args):
    batch_size = query_embeddings.size(0)
    question_hidden_states_np = query_embeddings.cpu().detach().numpy()

    all_context_input_ids = []
    all_context_attention_mask = []
    all_doc_scores = []
    
    for query_idx in range(batch_size):
        # 对每个查询嵌入找到最相关的聚类
        question_text = tokenizer.decode(input_ids[query_idx], skip_special_tokens=True)
        # print("\nOriginal Question:", question_text)
        relevant_clusters = get_relevant_clusters(question_hidden_states_np[query_idx:query_idx+1], cluster_centers, num_clusters=args.num_relevant_clusters)
        
        query_doc_ids = []
        query_doc_scores = []
        
        for cluster_id in relevant_clusters:
            cluster_id = cluster_id.item()
            if cluster_id in indexes:
                D, I = indexes[cluster_id].search(question_hidden_states_np[query_idx:query_idx+1], args.n_docs)
                scores = 1.0 / (1.0 + D[0])
                query_doc_scores.extend(scores.tolist())
                for idx in I[0]:
                    global_idx = cluster_global_indices[cluster_id][idx]
                    query_doc_ids.append(global_idx)
        
        # 对当前查询的所有检索文档进行编码
        context_input_ids = []
        context_attention_mask = []
        docs=[]
        for idx in query_doc_ids:
            doc = corpus[idx]
            docs.append(doc)
            # print("Retrieved text:", idx, ":::",doc)  # 打印检索到的文本
            encoded_doc = tokenizer(doc, max_length=args.max_input_length, padding='max_length', truncation=True, return_tensors="pt")
            context_input_ids.append(encoded_doc['input_ids'].squeeze(0))
            context_attention_mask.append(encoded_doc['attention_mask'].squeeze(0))
        
        all_context_input_ids.append(torch.stack(context_input_ids))
        all_context_attention_mask.append(torch.stack(context_attention_mask))
        all_doc_scores.append(torch.tensor(query_doc_scores).to(args.device))
    
    context_input_ids = torch.stack(all_context_input_ids).view(batch_size * args.n_docs, -1)
    context_attention_mask = torch.stack(all_context_attention_mask).view(batch_size * args.n_docs, -1)
    doc_scores = torch.cat(all_doc_scores)
    
    if len(doc_scores.size()) == 1:
        doc_scores = doc_scores.unsqueeze(0)  # 如果是一维，增加一个批次维度
    doc_scores = doc_scores.view(batch_size, args.n_docs)
    return docs
 
if __name__ == "__main__":
    # main()
    test()
    
